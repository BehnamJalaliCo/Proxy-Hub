// Package fsutil writes files atomically: a temporary file in the same directory, fsync, then
// rename over the target, so a reader (Telemt's config watcher, a restarted agent) only ever sees
// the old or the new complete content, never a partial one.
package fsutil

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Owner optionally changes the owner of a written file. UID/GID -1 keep the current value.
type Owner struct {
	UID int
	GID int
}

// KeepOwner leaves ownership to the writing process.
var KeepOwner = Owner{UID: -1, GID: -1}

// WriteAtomic replaces path with data. The file gets mode perm (for secrets: 0o600) before any
// byte is written, so the content is never readable by others even for a moment.
func WriteAtomic(path string, data []byte, perm os.FileMode, owner Owner) (err error) {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temp file in %s: %w", dir, err)
	}
	tmpName := tmp.Name()
	defer func() {
		if err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
		}
	}()
	if err = tmp.Chmod(perm); err != nil {
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if owner.UID >= 0 || owner.GID >= 0 {
		if err = tmp.Chown(owner.UID, owner.GID); err != nil {
			return fmt.Errorf("chown temp file: %w", err)
		}
	}
	if _, err = tmp.Write(data); err != nil {
		return fmt.Errorf("write temp file: %w", err)
	}
	if err = tmp.Sync(); err != nil {
		return fmt.Errorf("sync temp file: %w", err)
	}
	if err = tmp.Close(); err != nil {
		return fmt.Errorf("close temp file: %w", err)
	}
	if err = os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename into place: %w", err)
	}
	return syncDir(dir)
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("open dir for sync: %w", err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil && !errors.Is(err, os.ErrInvalid) {
		return fmt.Errorf("sync dir: %w", err)
	}
	return nil
}

// ReadIfExists returns the file content, or nil and no error when the file does not exist.
func ReadIfExists(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	return data, err
}

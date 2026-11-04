// Package sysstat reads a few whole-machine counters from /proc (uptime, load, memory, CPUs).
// Nothing per process or per connection is read.
package sysstat

import (
	"bufio"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// Stats are node-level counters; a nil field could not be read.
type Stats struct {
	UptimeS           *float64
	Load1             *float64
	MemTotalBytes     *uint64
	MemAvailableBytes *uint64
	CPUCount          int
}

// Read collects Stats from procRoot (normally "/proc").
func Read(procRoot string) Stats {
	st := Stats{CPUCount: runtime.NumCPU()}
	if f := firstField(procRoot + "/uptime"); f != "" {
		if v, err := strconv.ParseFloat(f, 64); err == nil && v >= 0 {
			st.UptimeS = &v
		}
	}
	if f := firstField(procRoot + "/loadavg"); f != "" {
		if v, err := strconv.ParseFloat(f, 64); err == nil && v >= 0 {
			st.Load1 = &v
		}
	}
	st.MemTotalBytes, st.MemAvailableBytes = meminfo(procRoot + "/meminfo")
	return st
}

func firstField(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	fields := strings.Fields(string(data))
	if len(fields) == 0 {
		return ""
	}
	return fields[0]
}

func meminfo(path string) (total, available *uint64) {
	f, err := os.Open(path)
	if err != nil {
		return nil, nil
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 2 {
			continue
		}
		kb, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			continue
		}
		bytes := kb * 1024
		switch fields[0] {
		case "MemTotal:":
			total = &bytes
		case "MemAvailable:":
			available = &bytes
		}
	}
	return total, available
}

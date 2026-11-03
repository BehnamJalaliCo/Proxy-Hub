package telemt

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// This file is a deliberately small TOML reader for the subset the renderer writes. The invariant
// check parses the exact bytes that will be written and refuses anything outside that subset
// (multi-line strings, inline tables, dotted or quoted keys, dates as literals ...), so what is
// checked is exactly what Telemt will read; there is no construct it could read differently.

// Kind is a value type.
type Kind int

// Value kinds of the subset.
const (
	KindString Kind = iota + 1
	KindInt
	KindBool
	KindArray
)

// Value is one parsed TOML value.
type Value struct {
	Kind  Kind
	Str   string
	Int   int64
	Bool  bool
	Items []Value
}

// Table is one TOML table: key -> value, in file order.
type Table struct {
	Keys   []string
	Values map[string]Value
}

func newTable() *Table { return &Table{Values: map[string]Value{}} }

// Doc is a parsed file: standard tables by dotted name ("" is the root) and arrays of tables.
type Doc struct {
	Tables map[string]*Table
	Arrays map[string][]*Table
}

const maxConfigBytes = 8 << 20 // Telemt's own limit for a config source

var (
	bareKeyRE   = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	tableNameRE = regexp.MustCompile(`^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$`)
	intRE       = regexp.MustCompile(`^[+-]?(?:0|[1-9][0-9]*)$`)
)

// ParseSubset parses data, or fails on the first construct outside the subset.
func ParseSubset(data []byte) (*Doc, error) {
	if len(data) > maxConfigBytes {
		return nil, errors.New("config too large")
	}
	if !utf8.Valid(data) {
		return nil, errors.New("config is not valid UTF-8")
	}
	doc := &Doc{Tables: map[string]*Table{"": newTable()}, Arrays: map[string][]*Table{}}
	explicit := map[string]bool{}
	current := doc.Tables[""]
	for i, raw := range strings.Split(string(data), "\n") {
		lineNo := i + 1
		if strings.ContainsAny(raw, "\r\x00") {
			return nil, fmt.Errorf("line %d: control character", lineNo)
		}
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// Telemt expands `include = "..."` lines before it parses TOML; never allow one.
		if rest, ok := strings.CutPrefix(line, "include"); ok && strings.HasPrefix(strings.TrimSpace(rest), "=") {
			return nil, fmt.Errorf("line %d: include directive refused", lineNo)
		}
		switch {
		case strings.HasPrefix(line, "[["):
			name, err := header(line, "[[", "]]")
			if err != nil {
				return nil, fmt.Errorf("line %d: %w", lineNo, err)
			}
			if _, clash := doc.Tables[name]; clash {
				return nil, fmt.Errorf("line %d: [[%s]] clashes with a table", lineNo, name)
			}
			t := newTable()
			doc.Arrays[name] = append(doc.Arrays[name], t)
			current = t
		case strings.HasPrefix(line, "["):
			name, err := header(line, "[", "]")
			if err != nil {
				return nil, fmt.Errorf("line %d: %w", lineNo, err)
			}
			if explicit[name] {
				return nil, fmt.Errorf("line %d: table [%s] defined twice", lineNo, name)
			}
			if _, clash := doc.Arrays[name]; clash {
				return nil, fmt.Errorf("line %d: [%s] clashes with an array of tables", lineNo, name)
			}
			explicit[name] = true
			t, ok := doc.Tables[name]
			if !ok {
				t = newTable()
				doc.Tables[name] = t
			}
			current = t
		default:
			key, value, err := keyValue(line)
			if err != nil {
				return nil, fmt.Errorf("line %d: %w", lineNo, err)
			}
			if _, dup := current.Values[key]; dup {
				return nil, fmt.Errorf("line %d: key %q defined twice", lineNo, key)
			}
			current.Keys = append(current.Keys, key)
			current.Values[key] = value
		}
	}
	// A key may not also be the name of a table (TOML: "key already defined").
	for name := range doc.Tables {
		if err := checkNoKeyTableClash(doc, name); err != nil {
			return nil, err
		}
	}
	for name := range doc.Arrays {
		if err := checkNoKeyTableClash(doc, name); err != nil {
			return nil, err
		}
	}
	return doc, nil
}

func checkNoKeyTableClash(doc *Doc, name string) error {
	if name == "" {
		return nil
	}
	parent, last := "", name
	if i := strings.LastIndex(name, "."); i >= 0 {
		parent, last = name[:i], name[i+1:]
	}
	if t, ok := doc.Tables[parent]; ok {
		if _, clash := t.Values[last]; clash {
			return fmt.Errorf("table %q clashes with a key", name)
		}
	}
	return nil
}

func header(line, open, close string) (string, error) {
	body, _, _ := strings.Cut(line, "#")
	body = strings.TrimSpace(body)
	if !strings.HasPrefix(body, open) || !strings.HasSuffix(body, close) {
		return "", errors.New("malformed table header")
	}
	name := strings.TrimSpace(body[len(open) : len(body)-len(close)])
	if !tableNameRE.MatchString(name) {
		return "", errors.New("table names must be dotted bare keys")
	}
	return name, nil
}

func keyValue(line string) (string, Value, error) {
	rawKey, rest, ok := strings.Cut(line, "=")
	if !ok {
		return "", Value{}, errors.New("expected key = value")
	}
	key := strings.TrimSpace(rawKey)
	if !bareKeyRE.MatchString(key) {
		return "", Value{}, errors.New("only plain bare keys are allowed (no dots, no quotes)")
	}
	value, tail, err := parseValue(strings.TrimSpace(rest))
	if err != nil {
		return "", Value{}, fmt.Errorf("key %s: %w", key, err)
	}
	tail = strings.TrimSpace(tail)
	if tail != "" && !strings.HasPrefix(tail, "#") {
		return "", Value{}, fmt.Errorf("key %s: unexpected text after value", key)
	}
	return key, value, nil
}

// parseValue reads one value from the start of s and returns the rest of the line.
func parseValue(s string) (Value, string, error) {
	switch {
	case strings.HasPrefix(s, `"""`) || strings.HasPrefix(s, "'"):
		return Value{}, "", errors.New("only basic single-line strings are allowed")
	case strings.HasPrefix(s, `"`):
		str, rest, err := parseBasicString(s)
		return Value{Kind: KindString, Str: str}, rest, err
	case strings.HasPrefix(s, "["):
		return parseArray(s)
	case strings.HasPrefix(s, "{"):
		return Value{}, "", errors.New("inline tables are not allowed")
	}
	end := strings.IndexAny(s, " \t,]#")
	if end < 0 {
		end = len(s)
	}
	token, rest := s[:end], s[end:]
	switch {
	case token == "true" || token == "false":
		return Value{Kind: KindBool, Bool: token == "true"}, rest, nil
	case intRE.MatchString(token):
		n, err := strconv.ParseInt(token, 10, 64)
		if err != nil {
			return Value{}, "", errors.New("integer out of range")
		}
		return Value{Kind: KindInt, Int: n}, rest, nil
	}
	return Value{}, "", errors.New("unsupported value (strings, integers, booleans and arrays only)")
}

func parseBasicString(s string) (string, string, error) {
	var b strings.Builder
	for i := 1; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '"':
			return b.String(), s[i+1:], nil
		case c == '\\':
			if i+1 >= len(s) {
				return "", "", errors.New("unterminated escape")
			}
			i++
			switch s[i] {
			case '"':
				b.WriteByte('"')
			case '\\':
				b.WriteByte('\\')
			case 'n':
				b.WriteByte('\n')
			case 't':
				b.WriteByte('\t')
			case 'r':
				b.WriteByte('\r')
			case 'b':
				b.WriteByte('\b')
			case 'f':
				b.WriteByte('\f')
			case 'u', 'U':
				width := 4
				if s[i] == 'U' {
					width = 8
				}
				if i+width >= len(s) {
					return "", "", errors.New("short unicode escape")
				}
				code, err := strconv.ParseUint(s[i+1:i+1+width], 16, 32)
				if err != nil || !utf8.ValidRune(rune(code)) {
					return "", "", errors.New("bad unicode escape")
				}
				b.WriteRune(rune(code))
				i += width
			default:
				return "", "", errors.New("unknown escape")
			}
		case c < 0x20 || c == 0x7f:
			return "", "", errors.New("control character in string")
		default:
			b.WriteByte(c)
		}
	}
	return "", "", errors.New("unterminated string")
}

func parseArray(s string) (Value, string, error) {
	rest := strings.TrimSpace(s[1:])
	arr := Value{Kind: KindArray}
	for {
		if strings.HasPrefix(rest, "]") {
			return arr, rest[1:], nil
		}
		if rest == "" {
			return Value{}, "", errors.New("unterminated array (arrays must be on one line)")
		}
		item, after, err := parseValue(rest)
		if err != nil {
			return Value{}, "", err
		}
		if item.Kind == KindArray {
			return Value{}, "", errors.New("nested arrays are not allowed")
		}
		if len(arr.Items) > 0 && arr.Items[0].Kind != item.Kind {
			return Value{}, "", errors.New("mixed array")
		}
		arr.Items = append(arr.Items, item)
		rest = strings.TrimSpace(after)
		if strings.HasPrefix(rest, ",") {
			rest = strings.TrimSpace(rest[1:])
		} else if !strings.HasPrefix(rest, "]") {
			return Value{}, "", errors.New("expected , or ] in array")
		}
	}
}

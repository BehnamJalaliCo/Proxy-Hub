package xray

import (
	"errors"
)

// A minimal protocol-buffers wire codec for the few Xray API messages the agent uses (the agent is
// standard library only). Field numbers are those of Xray 26.10.31's own descriptors:
//
//	xray.app.stats.command.QueryStatsRequest   {string pattern = 1; bool reset = 2}
//	xray.app.stats.command.QueryStatsResponse  {repeated Stat stat = 1}
//	xray.app.stats.command.Stat                {string name = 1; int64 value = 2}
//	xray.app.stats.command.SysStatsResponse    {... uint32 Uptime = 10}
//	xray.app.proxyman.command.AlterInboundRequest {string tag = 1; TypedMessage operation = 2}
//	xray.app.proxyman.command.AddUserOperation    {User user = 1}
//	xray.app.proxyman.command.RemoveUserOperation {string email = 1}
//	xray.common.protocol.User     {uint32 level = 1; string email = 2; TypedMessage account = 3}
//	xray.common.serial.TypedMessage {string type = 1; bytes value = 2}
//	xray.proxy.vless.Account      {string id = 1; string flow = 2; string encryption = 3}

const (
	wireVarint = 0
	wire64     = 1
	wireBytes  = 2
	wire32     = 5
)

var errMalformed = errors.New("xray api: malformed protobuf")

func appendVarint(b []byte, v uint64) []byte {
	for v >= 0x80 {
		b = append(b, byte(v)|0x80)
		v >>= 7
	}
	return append(b, byte(v))
}

func appendKey(b []byte, field, wire int) []byte {
	return appendVarint(b, uint64(field)<<3|uint64(wire))
}

// appendString writes a string (or bytes) field; proto3 leaves empty values out.
func appendString(b []byte, field int, s string) []byte {
	if s == "" {
		return b
	}
	b = appendKey(b, field, wireBytes)
	b = appendVarint(b, uint64(len(s)))
	return append(b, s...)
}

// appendMessage writes an embedded message field (always present, even when empty).
func appendMessage(b []byte, field int, m []byte) []byte {
	b = appendKey(b, field, wireBytes)
	b = appendVarint(b, uint64(len(m)))
	return append(b, m...)
}

func appendUint(b []byte, field int, v uint64) []byte {
	if v == 0 {
		return b
	}
	b = appendKey(b, field, wireVarint)
	return appendVarint(b, v)
}

func appendBool(b []byte, field int, v bool) []byte {
	if !v {
		return b
	}
	b = appendKey(b, field, wireVarint)
	return append(b, 1)
}

func readVarint(b []byte) (uint64, int, error) {
	var v uint64
	for i := 0; i < len(b) && i < 10; i++ {
		c := b[i]
		v |= uint64(c&0x7f) << (7 * uint(i))
		if c < 0x80 {
			return v, i + 1, nil
		}
	}
	return 0, 0, errMalformed
}

// field is one decoded field: varint value or length-delimited bytes.
type field struct {
	num   int
	wire  int
	value uint64
	bytes []byte
}

// parseFields decodes one message level. Unknown fields are kept (and ignored by callers); groups
// (wire types 3 and 4) do not exist in Xray's protos and are refused.
func parseFields(b []byte) ([]field, error) {
	var out []field
	for len(b) > 0 {
		key, n, err := readVarint(b)
		if err != nil {
			return nil, err
		}
		b = b[n:]
		f := field{num: int(key >> 3), wire: int(key & 7)}
		if f.num <= 0 {
			return nil, errMalformed
		}
		switch f.wire {
		case wireVarint:
			v, n, err := readVarint(b)
			if err != nil {
				return nil, err
			}
			f.value, b = v, b[n:]
		case wire64:
			if len(b) < 8 {
				return nil, errMalformed
			}
			b = b[8:]
		case wire32:
			if len(b) < 4 {
				return nil, errMalformed
			}
			b = b[4:]
		case wireBytes:
			l, n, err := readVarint(b)
			if err != nil || uint64(len(b)-n) < l {
				return nil, errMalformed
			}
			f.bytes, b = b[n:n+int(l)], b[n+int(l):]
		default:
			return nil, errMalformed
		}
		out = append(out, f)
	}
	return out, nil
}

// --- messages ---

func typedMessage(typ string, value []byte) []byte {
	var b []byte
	b = appendString(b, 1, typ)
	return appendString(b, 2, string(value))
}

func encodeQueryStats(pattern string, reset bool) []byte {
	var b []byte
	b = appendString(b, 1, pattern)
	return appendBool(b, 2, reset)
}

// decodeQueryStats returns name -> value of every Stat in a QueryStatsResponse.
func decodeQueryStats(b []byte) (map[string]int64, error) {
	top, err := parseFields(b)
	if err != nil {
		return nil, err
	}
	out := map[string]int64{}
	for _, f := range top {
		if f.num != 1 || f.wire != wireBytes {
			continue
		}
		inner, err := parseFields(f.bytes)
		if err != nil {
			return nil, err
		}
		var name string
		var value int64
		for _, g := range inner {
			switch {
			case g.num == 1 && g.wire == wireBytes:
				name = string(g.bytes)
			case g.num == 2 && g.wire == wireVarint:
				value = int64(g.value) //nolint:gosec // int64 on the wire, two's complement
			}
		}
		out[name] = value
	}
	return out, nil
}

// decodeSysUptime returns SysStatsResponse.Uptime (seconds).
func decodeSysUptime(b []byte) (uint32, error) {
	fields, err := parseFields(b)
	if err != nil {
		return 0, err
	}
	for _, f := range fields {
		if f.num == 10 && f.wire == wireVarint {
			return uint32(f.value), nil //nolint:gosec // uint32 on the wire
		}
	}
	return 0, nil
}

func encodeAddUser(tag, flow string, u User) []byte {
	var account []byte
	account = appendString(account, 1, u.ID)
	if flow != "" {
		account = appendString(account, 2, flow)
	}
	// encryption (3) stays empty, exactly as for a client from the config file: the inbound's
	// "decryption": "none" applies.
	var user []byte
	user = appendUint(user, 1, userLevel)
	user = appendString(user, 2, u.Email)
	user = appendMessage(user, 3, typedMessage("xray.proxy.vless.Account", account))
	op := appendMessage(nil, 1, user)
	return encodeAlterInbound(tag, typedMessage("xray.app.proxyman.command.AddUserOperation", op))
}

func encodeRemoveUser(tag, email string) []byte {
	op := appendString(nil, 1, email)
	return encodeAlterInbound(tag, typedMessage("xray.app.proxyman.command.RemoveUserOperation", op))
}

func encodeAlterInbound(tag string, operation []byte) []byte {
	var b []byte
	b = appendString(b, 1, tag)
	return appendMessage(b, 2, operation)
}

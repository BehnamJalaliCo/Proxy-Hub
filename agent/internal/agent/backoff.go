package agent

import (
	"math/rand/v2"
	"time"
)

// backoff grows exponentially between failed panel calls, capped, and resets on success.
type backoff struct {
	min, max, cur time.Duration
}

func newBackoff(min, max time.Duration) *backoff { return &backoff{min: min, max: max} }

func (b *backoff) next() time.Duration {
	if b.cur == 0 {
		b.cur = b.min
	} else {
		b.cur *= 2
		if b.cur > b.max {
			b.cur = b.max
		}
	}
	return b.cur
}

func (b *backoff) reset() { b.cur = 0 }

// jitter spreads polls of many nodes by +-10% so they do not hit the panel in lockstep.
func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return d
	}
	span := int64(d) / 5
	if span <= 0 {
		return d
	}
	return d - time.Duration(span/2) + time.Duration(rand.Int64N(span)) //nolint:gosec // timing jitter, not security
}

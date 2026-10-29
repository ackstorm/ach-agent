// SPDX-License-Identifier: MIT

package controller

import "time"

// Eventually polls cond at the supplied interval until it returns true or the timeout
// elapses. Returns true on success, false on timeout.
func Eventually(cond func() bool, timeout, interval time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(interval)
	}
	return cond()
}

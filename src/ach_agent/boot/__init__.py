# SPDX-License-Identifier: Apache-2.0
"""Process boot concerns: paths, stores, prompt assembly, secrets, health state.

This package sits OUTSIDE the event path (channels -> router -> engine); it is what
`main()` uses to stand the process up.
"""

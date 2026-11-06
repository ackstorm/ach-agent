#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Scripted stand-in for `pi --mode rpc`. FAKE_MODE selects the misbehaviour."""
import json
import os
import sys
import time

mode = os.environ["FAKE_MODE"]


def out(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


stats_calls = 0
for line in sys.stdin:
    command = json.loads(line)
    kind, request_id = command["type"], command["id"]
    if kind == "get_state":
        out({"type": "response", "id": request_id, "success": True,
             "data": {"sessionId": "s1", "model": {"id": os.environ["FAKE_MODEL"]}}})
    elif kind == "get_session_stats":
        stats_calls += 1
        if mode == "stdin-closed-before-prompt":
            out({"type": "response", "id": request_id, "success": True, "data": {}})
            os.close(0)
            time.sleep(1)
            os._exit(1)
        null_tokens = mode == "null-tokens" and stats_calls == 2
        out({"type": "response", "id": request_id, "success": True,
             "data": {"tokens": {"input": None if null_tokens else 0}}})
    elif kind == "prompt":
        out({"type": "response", "id": request_id, "success": True})
        if mode == "non-json":
            sys.stdout.write("warning: not json\n")
            sys.stdout.flush()
            time.sleep(600)
        out({"type": "agent_settled", "aborted": False})
    elif kind == "get_last_assistant_text":
        if mode == "die-after-settled":
            os._exit(137)
        out({"type": "response", "id": request_id, "success": True, "data": {"text": "pong"}})

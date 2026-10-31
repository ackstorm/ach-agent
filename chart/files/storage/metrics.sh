#!/bin/sh
# Answers the one HTTP request on the connection nc hands over with the gauges reconcile.sh writes.
# Reads the whole request first: closing with unread bytes resets the connection.
cr=$(printf '\r')
while IFS= read -r line; do
  [ -z "${line%"$cr"}" ] && break
done
printf 'HTTP/1.0 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n\r\n'
cat /tmp/metrics

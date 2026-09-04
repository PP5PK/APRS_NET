#!/bin/bash
for port in 587 465 2525 25; do
  timeout 5 bash -c "echo > /dev/tcp/smtp-relay.brevo.com/$port" \
    && echo "Port $port: OPEN" || echo "Port $port: BLOCKED"
done

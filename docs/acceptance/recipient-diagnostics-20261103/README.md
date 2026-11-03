# Recipient installation and empty-service diagnostics

The user reports that R7 fails during preparation on Leng An's computer and that
A Jiu can send WeCom messages but sees an empty service list. Neither recipient
machine has been directly inspected. These incidents remain unresolved.

The installer previously reduced payload validation, Office validation, backup,
process enumeration/termination and update-uninstall failures to exit code 1.
The helper now accepts an append-only log path, records preparation stages and
retains exception traces and captured subprocess output. NSIS passes a durable
per-user log destination. A bootloader failure before Python starts still cannot
produce this Python log; the dialog explicitly qualifies its availability.

A read-only Windows PowerShell 5.1 tool collects OS/build, relevant executable
paths/hashes, network profiles, firewall policy, allowlisted local discovery and
service fields, provider reachability, and hashes of any still-extracted NSIS
payload. It does not stop processes, uninstall, change settings/firewall rules,
send messages, or copy tokens, databases or raw application logs. The report may
contain internal IP addresses and device names. Keep it within support.

Validation:
- 17 installer tests passed using an isolated pytest base directory. The first
  run's assertions passed but pytest's shared temporary-directory cleanup failed
  with Windows access denied; the isolated rerun exited successfully.
- The NSIS source compiled successfully with stub assets (syntax validation only;
  this is not an installable product or full-installer acceptance).
- Windows PowerShell 5.1 ran the diagnostic tool successfully on the local host:
  discovery enabled/running, two discovered peers, five local services, company
  health HTTP 200, both observed provider addresses HTTP 200. This does not prove
  either address is reachable from the recipients.
- No installed application was changed and no replacement full installer was
  published. The diagnostic ZIP is packaged from committed develop sources.

Next evidence: run the diagnostic tool on each affected machine. On Leng An's
computer keep the failed install dialog open to retain the temporary payload.
Return the generated desktop JSON files, labelled by recipient, for root-cause
analysis. Do not treat diagnostic instrumentation as a fix for either incident.

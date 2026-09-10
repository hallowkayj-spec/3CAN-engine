# Windows command transport

The Windows hook commands require Codex's native Windows PowerShell shell and
invoke the existing launcher directly:

```powershell
& (Join-Path $env:PLUGIN_ROOT 'hooks/run_runtimehook.ps1')
```

`SessionStart` appends `-SessionOrientation`. There is no embedded machine path,
encoded command, extra permission, policy bypass, network request or second
launcher. The tests execute native PowerShell argv with a spaced plugin path
and preserve Unicode input/output and the inactive fast path. A custom cmd or
Git Bash shell on Windows is not a validated plugin configuration.

The former `%SystemRoot%` / `%PLUGIN_ROOT%` command used cmd syntax. Python's
`shell=True` test selected cmd and passed, while native Codex selected PowerShell
and failed before the launcher. The upstream cmd quoting issue #32402 was an
initial hypothesis, not the demonstrated local cause; a quote-free command
still failed. Reproducing the selected shell exposed the actual mismatch.
Removing the batch wrapper leaves one Windows implementation. Native hook trust
must be reviewed again when the definition changes; installation is not proof
that lifecycle execution or semantic review happened.

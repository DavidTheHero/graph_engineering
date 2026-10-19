# Background console launch fix, 19 October 2026

The scheduled controller already used `pythonw.exe`, but repository/test and
OpenCode launchers only requested a new process group. Git, GitHub CLI, version
and cancellation helpers used default launch settings. These could allocate a
visible console when called from the windowless controller.

`processes.background_options` now supplies hidden Windows startup information
and a private console inherited by command descendants. POSIX process-session
behavior is preserved. Standard output/error capture, cancellation, credentials
scrubbing and execution limits are unchanged.

Five launch-policy regression tests failed before the correction. The corrected
suite also runs a real parent/child command and checks both console windows are
invisible. Existing real timeout/cancellation tests still pass.

The controller was paused for maintenance, restarted only after active work
checkpointed, and admission restored. The fresh scheduler receipt is in
`restart.json`. Full backend, browser, compilation and motion results are in
`checks/`; these results are separate from the earlier autonomy demonstration.

# Agent progress and session-lock contention on Windows

On 2026-11-04 at 10:38 Hong Kong time, the local node logged two consecutive session transactions on threads asyncio_9 and asyncio_3. The first took 7.094 s to obtain its working state and 11.047 s in the write phase. The second spent 10.734 s copying its return value. These phases ran under the exclusive session lock. A simultaneous state inspection waited 26.469 s. The desktop read deadline was 10 s. The original diagnostics identify threads and phases but omit callback names.

Task execution, cancellation checks, list/detail polling, and a two-second automation tick shared the lock. An idle tick opened a transaction even when the disk schedule matched and nothing was due. The fix checks idle schedules without opening a transaction. A writer prepares a private copy of a committed snapshot before taking the lock, checks its identity under the lock, and refreshes the working state if another writer committed. The callback runs once. Readers validate the committed root's file stamp, and copying a committed result no longer holds up the next writer. Slow transaction logs include the code-defined callback name.

## Verification

- Injected an 11 s pause inside a writer against a synthetic task, then called the normal runtime list and detail methods. On baseline 1f87ec5 the list took 11001.7 ms. On the fix, list and detail took 0.3 ms each while the writer was still paused. No model or network request was involved.
- Regression tests cover list, detail, and control visibility during a write, the next writer during slow result copying, concurrent commits across two store instances without replay, an idle scheduler, migration, and recovery from another process's commit.
- The automation, storage, submission and runtime selection passed 60 cases. The storage and streaming selection passed 51 cases, including the result-copy regression added after the first selection. These selections overlap.
- Three transcript compaction tests failed on both the original develop commit and the candidate. They are excluded from the first passing selection.

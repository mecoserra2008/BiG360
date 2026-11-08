"""Stop CN forward/reverse BaoStock workers of a shard once their done-files cover the whole shard (they walk the same
shard from both ends and would otherwise redo each other's symbols). `python -m app.ingestion.price_coverage_cnwatch`."""

from __future__ import annotations

import os
import signal
import sys
import time

from app.ingestion.price_coverage import cn_list, load_state, read_done, update_state


def shard_left(worker: int, workers: int = 3) -> int:
    syms = sorted(cn_list(max_age_h=24 * 30).get_column("yf_ticker").to_list())
    mine = {x for i, x in enumerate(syms) if i % workers == worker}
    done = read_done(f"p1_cn_w{worker}") | read_done(f"p1_cn_w{worker}_rev")
    return len(mine - done)


def main() -> int:
    open_shards = {0, 1, 2}
    while open_shards:
        tasks = load_state().get("tasks", {})
        for w in sorted(open_shards):
            left = shard_left(w)
            if left == 0:
                for t in (f"p1_cn_w{w}", f"p1_cn_w{w}_rev"):
                    st = tasks.get(t) or {}
                    pid = st.get("pid")
                    if pid and st.get("status") == "running":
                        try:
                            os.kill(int(pid), signal.SIGTERM)
                        except OSError:
                            pass
                        update_state(t, None, status="done", note="shard complete (forward + reverse done-files)")
                open_shards.discard(w)
                print(f"shard {w} complete", flush=True)
        if open_shards:
            time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

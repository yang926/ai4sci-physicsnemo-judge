"""Narrow service lifecycle boundary for the separate operator application."""
import json
import subprocess
import threading


class ControlError(RuntimeError):
    """The service controller could not establish a safe lifecycle state."""


class SystemdController:
    """Invoke a root-owned, fixed-action helper, never a shell or user arguments.

    The helper must emit JSON containing boolean ``intake_paused`` and
    ``workers_stopped`` fields. ``pause`` stops API/enrollment, ``quiesce``
    stops workers, and ``resume`` starts workers, API, then enrollment.
    """

    HELPER = "/usr/local/sbin/ai4sci-judge-control"

    def _call(self, action):
        if action not in {"status", "pause", "quiesce", "resume"}:
            raise ControlError("Unsupported maintenance action.")
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", self.HELPER, action],
                capture_output=True, text=True, check=True, timeout=180,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
            value = json.loads(result.stdout)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise ControlError("Judge service control failed; check the server service log.") from exc
        if (not isinstance(value, dict)
                or type(value.get("intake_paused")) is not bool
                or type(value.get("workers_stopped")) is not bool):
            raise ControlError("Invalid maintenance service status.")
        return value

    def status(self):
        return self._call("status")

    def pause(self):
        return self._call("pause")

    def quiesce(self):
        return self._call("quiesce")

    def resume(self):
        return self._call("resume")


class MemoryController:
    """In-process lifecycle simulation. ONLY for isolated rehearsal databases.

    There are no real workers or API writers in this mode. A fresh process
    starts accepting rehearsal operations; this must never control live state.
    """

    def __init__(self):
        self._paused = False
        self._stopped = False
        self._lock = threading.RLock()

    def status(self):
        with self._lock:
            return {"intake_paused": self._paused, "workers_stopped": self._stopped,
                    "services": {}, "simulated": True}

    def pause(self):
        with self._lock:
            self._paused = True
            return self.status()

    def quiesce(self):
        with self._lock:
            if not self._paused:
                raise ControlError("Pause submissions before stopping rehearsal workers.")
            self._stopped = True
            return self.status()

    def resume(self):
        with self._lock:
            self._paused = False
            self._stopped = False
            return self.status()

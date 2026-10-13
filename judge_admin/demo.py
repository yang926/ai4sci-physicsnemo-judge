"""Explicit synthetic fixtures in a newly created, marked, isolated state only."""
import json
import time
import uuid

from ai4sci_judge.store import Store


def initialize_demo(directory):
    store = Store.initialize(directory, steps=2, device="cpu")
    marker = store.directory / "DEMO_ONLY"
    with marker.open("x") as file:
        file.write("Synthetic projector/admin rehearsal. Never use for event submissions.\n")
    marker.chmod(0o600)
    _, revision = store.settings()
    now = time.time()
    for index in range(1, 31):
        token = store.add_participant(f"DEMO Learner {index:02d}")
        participant = store.authenticate(token)["id"]
        with store.connect() as db:
            for challenge in range(1, 5):
                if index % 7 == 0 and challenge > 2:
                    continue  # Unsubmitted Challenge is different from a scored zero.
                score = 100 if index <= 2 else (index * 17 + challenge * 11) % 101
                for attempt in range(1, 3 if index == 3 else 2):
                    actual = max(0, score - 10) if attempt == 1 and index == 3 else score
                    db.execute("INSERT INTO submissions (id,participant,challenge,source_hash,sources,status,created,started,finished,score,result,fingerprint) VALUES (?,?,?,?,?,'completed',?,?,?,?,?,?)",
                               (uuid.uuid4().hex, participant, str(challenge), f"demo-{index}-{challenge}-{attempt}", "{}",
                                now + attempt, now + attempt, now + attempt, actual,
                                json.dumps({"demo": True, "score": actual, "description": "Synthetic display fixture, not a grading result"}), revision))
    return store

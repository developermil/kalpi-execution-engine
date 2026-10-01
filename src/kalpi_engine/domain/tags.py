import base64
import hashlib

TAG_MIN_LEN = 8
TAG_MAX_LEN = 20
TAG_DEFAULT_LEN = 16


def _check_len(length: int) -> int:
    if not TAG_MIN_LEN <= length <= TAG_MAX_LEN:
        raise ValueError(f"tag length {length} outside {TAG_MIN_LEN}..{TAG_MAX_LEN}")
    return length


def tag_length(broker_tag_max_len: int) -> int:
    """length = min(16, meta.tag_max_len), which must land in 8..20 (SPEC §3)."""
    return _check_len(min(TAG_DEFAULT_LEN, broker_tag_max_len))


def make_tag(run_id: str, leg_index: int, length: int = TAG_DEFAULT_LEN) -> str:
    """Deterministic alphanumeric order tag, used to reconcile ambiguous submits."""
    _check_len(length)
    digest = hashlib.sha1(f"{run_id}:{leg_index}".encode()).digest()
    return "K" + base64.b32encode(digest).decode("ascii")[: length - 1]

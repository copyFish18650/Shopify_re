"""Parse mailbox imports without exposing passwords or optional OAuth fields."""
import re


def parse_mailboxes(text):
    if not isinstance(text, str):
        raise ValueError("邮箱文本格式无效")
    lines = [(i, line.strip()) for i, line in enumerate(text.lstrip("\ufeff").splitlines(), 1) if line.strip()]
    if not 1 <= len(lines) <= 1000:
        raise ValueError("每次可导入 1–1000 行邮箱")
    records, seen, duplicates, ignored_extra = [], {}, 0, 0
    for number, line in lines:
        fields = [value.strip() for value in line.split("----")]
        if len(fields) not in (2, 4):
            raise ValueError("第 {} 行格式错误，应为：邮箱----密码".format(number))
        email, password = fields[:2]
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("第 {} 行邮箱格式无效".format(number))
        if not 1 <= len(password) <= 500 or any(ord(c) < 32 for c in password):
            raise ValueError("第 {} 行密码为空、过长或包含控制字符".format(number))
        key = email.casefold()
        if key in seen:
            if seen[key] != password:
                raise ValueError("第 {} 行邮箱重复且密码不同，请核对后再导入".format(number))
            duplicates += 1
        else:
            seen[key] = password
            records.append({"email": email, "password": password})
        ignored_extra += len(fields) == 4
    return records, {"duplicates": duplicates, "ignored_extra": ignored_extra}


def mailbox_usage(email, jobs, mailbox=None):
    mailbox = mailbox or {}
    revision = mailbox.get("usage_revision", 0)
    matching = [j for j in jobs if j["payload"]["email"].casefold() == email.casefold()]
    def started(job):
        payload = job["payload"]
        if "execution_started" in payload:
            return bool(payload["execution_started"])
        return job["state"] in ("running", "waiting", "success") or (
            bool(payload.get("profile_id")) and job["state"] in ("failed", "interrupted"))
    busy = [j for j in matching if j["state"] in ("queued", "paused", "preparing", "running", "waiting", "stopping", "interrupted")]
    # A manual change starts a new usage revision. Keep old tasks for history,
    # but only subsequent submissions/retries can automatically mark it used again.
    used = [j for j in matching if started(j) and j["payload"].get("mailbox_usage_revision", 0) >= revision]
    job = (busy or used or matching or [None])[0]
    state = "in_use" if any(started(j) for j in busy) else "reserved" if busy else "used" if used else mailbox.get("usage_state", "unused")
    return {"state": state, "job_id": job["id"] if job else "", "job_state": job["state"] if job else "",
            "used_at": (used[-1]["payload"].get("execution_started") or used[-1]["created"]) if used
            else mailbox.get("usage_updated", "") if state == "used" else ""}

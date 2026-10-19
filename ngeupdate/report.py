"""Final-output formatting for a finished deploy.

Pure functions over the job snapshot — no I/O, no globals. `format_deploy_result`
is the only public entry point and the only thing `mcp.py` needs from this
module.
"""


def _fmt_duration(seconds):
    s = int(round(seconds))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm%02ds" % (h, m, s)


def _is_under(file_path, dir_prefix):
    """True if ``file_path`` equals ``dir_prefix`` or lives under it.

    Refuses any directory whose name is a strict prefix of the path's first
    segment (e.g. ``migrations`` won't match ``migrations_old/x``).
    """
    return file_path == dir_prefix or file_path.startswith(dir_prefix + "/")


def _migrate_seed_notice_lines(
    diff_files, migrate_paths, seed_paths, migrate_requested, seed_requested,
):
    """Build the human-readable notice appended to the deploy output.

    Rules (from plan §"Biar agent mana pun notice"):
      - Path config present + pull brought files under it + flag NOT set → ⚠️
      - Path config present + no relevant changes + flag NOT set → ℹ️
      - Flag requested → no notice for that channel, whatever the outcome.
        The step list already shows whether it ran; a "TIDAK dijalankan"
        line after a seed that DID run would invite a duplicate seed.

    Returns a list of strings (each one ≤ 1 line) suitable for direct append.
    Empty list = nothing to say.
    """
    lines = []

    def _for(prefixes, label, requested):
        if not prefixes or requested:
            return
        primary = prefixes[0]
        rest = " (paths: %s)" % ", ".join(prefixes) if len(prefixes) > 1 else ""
        changed = [
            f for f in diff_files
            if any(_is_under(f, p) for p in prefixes)
        ]
        if changed:
            lines.append(
                "⚠️ Pull ini membawa perubahan di %s (%d file) tapi %s TIDAK "
                "dijalankan. Kalau perlu: deploy ulang dengan %s=true.%s"
                % (primary, len(changed), label, label, rest)
            )
        else:
            lines.append(
                "ℹ️ Project ini support %s%s; tidak dijalankan."
                % (label, rest)
            )

    _for(migrate_paths, "migrate", migrate_requested)
    _for(seed_paths, "seed", seed_requested)
    return lines


def format_deploy_result(
    project_path, mode, steps, ok, total_seconds,
    migrate_paths=None, seed_paths=None,
    migrate_requested=False, seed_requested=False,
    diff_files=None,
):
    """First line = clear status; rest = per-step details + notices. Always UTF-8."""
    if ok:
        status = "✅ DONE — %s deployed (%s) in %s" % (
            project_path,
            mode,
            _fmt_duration(total_seconds),
        )
    else:
        failed = steps[-1]
        label = " ".join(failed["command"])
        reason = "timeout" if failed["timed_out"] else "exit %d" % failed["returncode"]
        status = "❌ FAILED at %r (%s) — %s after %s" % (
            label,
            reason,
            project_path,
            _fmt_duration(total_seconds),
        )

    lines = [status, ""]
    for idx, step in enumerate(steps, start=1):
        cmd = " ".join(step["command"])
        rc = step["returncode"]
        if step["timed_out"]:
            outcome = "TIMEOUT"
        elif rc == 0:
            outcome = "ok"
        else:
            outcome = "exit %d" % rc
        lines.append("step %d: %s  [%s, %ss]" % (idx, cmd, outcome, step["elapsed_seconds"]))
        tail = step["output_tail"].rstrip()
        if tail:
            for ln in tail.splitlines()[-20:]:
                lines.append("    " + ln)
        lines.append("")

    # Migrate/seed notice. The caller (run_deploy) pre-computes diff_files
    # between the pre-pull and post-pull HEADs so this function stays pure.
    if migrate_paths or seed_paths:
        notes = _migrate_seed_notice_lines(
            diff_files or [], migrate_paths or [], seed_paths or [],
            migrate_requested, seed_requested,
        )
        for note in notes:
            lines.append(note)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
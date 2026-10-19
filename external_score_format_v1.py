"""Pure, fail-closed formatting for external research scores in CC.

No network, credentials, score mutations or order execution. The caller must
authenticate the source and provide a trusted read-only snapshot.
"""
def format_external_score(snapshot):
    if type(snapshot) is not dict:
        raise ValueError("INVALID_SCORE_SNAPSHOT")
    system = snapshot.get("system")
    expected = {"DRJ": "PAPER_ONLY_UNVERIFIED",
                "JJ Jame": "RESEARCH_UNVERIFIED"}
    if system not in expected or snapshot.get("classification") != expected[system]:
        raise ValueError("UNTRUSTED_SCORE_CLASSIFICATION")
    wins, losses, opened = (snapshot.get(k) for k in ("wins", "losses", "open"))
    if any(type(n) is not int or n < 0 for n in (wins, losses, opened)):
        raise ValueError("INVALID_SCORE_COUNTS")
    if snapshot.get("resolved") != wins + losses:
        raise ValueError("INCONSISTENT_SCORE_TOTAL")
    if system == "DRJ" and opened > 1:
        raise ValueError("INVALID_DRJ_OPEN_COUNT")
    rate = f"{100 * wins / (wins + losses):.2f}%" if wins + losses else "N/A"
    return "\n".join([
        f"💎 {system} • {'PAPER' if system == 'DRJ' else 'RESEARCH'} SCORE",
        f"🏁 WIN: {wins}",
        f"🛑 LOSS: {losses}",
        f"⏳ OPEN: {opened}",
        f"📈 Win Rate (resolved): {rate}",
        "⚠️ PAPER_ONLY • UNVERIFIED" if system == "DRJ" else "⚠️ RESEARCH • UNVERIFIED",
        "⚠️ ไม่ใช่ผลการเทรดจริง • ไม่ใช่คำแนะนำลงทุน",
    ])

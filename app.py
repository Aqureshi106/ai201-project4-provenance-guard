import json
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from provenance_logic import (
    build_transparency_label,
    combine_signals,
    groq_model_attribution_review,
    normalize_submission_payload,
    specificity_context_signal,
    stylometric_heuristics,
    unavailable_groq_signal,
)


load_dotenv()

app = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

LOG_PATH = "audit_log.jsonl"
CERTIFICATE_DURATION_DAYS = 90
VERIFIED_HUMAN_DISPLAY_TEXT = "Verified human creator"


def utc_timestamp(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def log_event(entry):
    entry["timestamp"] = utc_timestamp()
    with open(LOG_PATH, "a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(entry) + "\n")


def get_log(limit=20):
    try:
        with open(LOG_PATH, encoding="utf-8") as log_file:
            lines = log_file.readlines()
    except FileNotFoundError:
        return []

    if limit is None:
        selected_lines = lines
    else:
        selected_lines = lines[-limit:]

    return [json.loads(line) for line in selected_lines]


def is_attribution_decision(entry):
    return (
        entry.get("event_type") == "attribution_decision"
        or (
            entry.get("status") == "classified"
            and entry.get("attribution") in {"likely_ai", "likely_human", "uncertain"}
            and entry.get("confidence") is not None
        )
    )


def analytics_summary():
    entries = get_log(limit=None)
    decisions = [entry for entry in entries if is_attribution_decision(entry)]
    appeals = [
        entry
        for entry in entries
        if entry.get("event_type") == "appeal_submitted"
    ]

    detection_patterns = {
        "likely_ai": 0,
        "likely_human": 0,
        "uncertain": 0,
    }
    confidences = []
    verified_creator_decisions = 0

    for decision in decisions:
        attribution = decision.get("attribution")
        if attribution in detection_patterns:
            detection_patterns[attribution] += 1

        confidence = decision.get("confidence")
        if isinstance(confidence, (int, float)):
            confidences.append(float(confidence))

        if decision.get("creator_verified_human") is True:
            verified_creator_decisions += 1

    decision_count = len(decisions)
    appeal_count = len(appeals)
    appeal_rate = appeal_count / decision_count if decision_count else 0.0
    average_confidence = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )

    return {
        "decision_count": decision_count,
        "appeal_count": appeal_count,
        "appeal_rate": round(appeal_rate, 3),
        "appeal_rate_percent": round(appeal_rate * 100, 1),
        "detection_patterns": detection_patterns,
        "average_confidence": round(average_confidence, 3),
        "verified_creator_decisions": verified_creator_decisions,
    }


def find_latest_attribution_decision(content_id):
    for entry in reversed(get_log(limit=1000)):
        if (
            entry.get("content_id") == content_id
            and entry.get("attribution") is not None
            and entry.get("confidence") is not None
            and entry.get("status") == "classified"
        ):
            return entry
    return None


def active_human_certificate_for_creator(creator_id):
    now = utc_timestamp()
    for entry in reversed(get_log(limit=1000)):
        if (
            entry.get("event_type") == "human_certificate_issued"
            and entry.get("creator_id") == creator_id
            and entry.get("credential_type") == "verified_human"
            and entry.get("status") == "active"
            and entry.get("expires_at", "") > now
        ):
            return {
                "credential_id": entry.get("credential_id"),
                "credential_type": "verified_human",
                "creator_id": creator_id,
                "status": "active",
                "issued_at": entry.get("issued_at"),
                "expires_at": entry.get("expires_at"),
                "verification_method": entry.get("verification_method"),
                "display_text": VERIFIED_HUMAN_DISPLAY_TEXT,
            }
    return None


@app.route("/")
def home():
    return "Provenance Guard is running."


@app.route("/verify-human", methods=["POST"])
def verify_human():
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({"error": "Request body must be JSON."}), 400

    creator_id = data.get("creator_id")
    verification_method = data.get("verification_method")
    evidence_summary = data.get("evidence_summary")

    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "A non-empty creator_id field is required."}), 400

    if not isinstance(verification_method, str) or not verification_method.strip():
        return jsonify(
            {"error": "A non-empty verification_method field is required."}
        ), 400

    if not isinstance(evidence_summary, str) or not evidence_summary.strip():
        return jsonify(
            {"error": "A non-empty evidence_summary field is required."}
        ), 400

    issued_at_dt = datetime.now(timezone.utc)
    expires_at_dt = issued_at_dt + timedelta(days=CERTIFICATE_DURATION_DAYS)
    issued_at = utc_timestamp(issued_at_dt)
    expires_at = utc_timestamp(expires_at_dt)
    credential_id = str(uuid.uuid4())

    certificate = {
        "credential_id": credential_id,
        "credential_type": "verified_human",
        "creator_id": creator_id,
        "status": "active",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "verification_method": verification_method,
        "display_text": VERIFIED_HUMAN_DISPLAY_TEXT,
    }

    log_event(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "human_certificate_issued",
            "credential_id": credential_id,
            "credential_type": "verified_human",
            "creator_id": creator_id,
            "verification_method": verification_method,
            "evidence_summary": evidence_summary,
            "status": "active",
            "issued_at": issued_at,
            "expires_at": expires_at,
            "display_text": VERIFIED_HUMAN_DISPLAY_TEXT,
        }
    )

    return jsonify(
        {
            "message": "Verified human credential issued.",
            "certificate": certificate,
        }
    ), 201


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({"error": "Request body must be JSON."}), 400

    creator_id = data.get("creator_id")

    try:
        submission = normalize_submission_payload(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "A non-empty creator_id field is required."}), 400

    content_id = str(uuid.uuid4())
    analysis_text = submission["analysis_text"]
    try:
        groq_signal = groq_model_attribution_review(analysis_text)
    except Exception as exc:
        groq_signal = unavailable_groq_signal(str(exc))

    groq_signal_available = groq_signal.get("available", True)
    stylometric_signal = stylometric_heuristics(analysis_text)
    specificity_signal = specificity_context_signal(analysis_text)
    signals = [groq_signal, stylometric_signal, specificity_signal]
    signal_availability = {
        "groq_model_attribution_review": groq_signal_available,
        "stylometric_heuristics": True,
        "specificity_context_signal": True,
    }
    decision = combine_signals(*signals)
    attribution = decision["attribution"]
    confidence = decision["confidence"]
    label = build_transparency_label(attribution, confidence)
    provenance_certificate = active_human_certificate_for_creator(creator_id)
    display = {
        "transparency_label": label,
        "provenance_badge": (
            provenance_certificate["display_text"]
            if provenance_certificate is not None
            else None
        ),
    }

    log_event(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "attribution_decision",
            "content_id": content_id,
            "creator_id": creator_id,
            "content_type": submission["content_type"],
            "content_summary": submission["content_summary"],
            "attribution": attribution,
            "confidence": confidence,
            "combined_confidence_score": confidence,
            "llm_score": groq_signal["score"],
            "stylometric_score": stylometric_signal["score"],
            "specificity_context_score": specificity_signal["score"],
            "groq_signal_available": groq_signal_available,
            "signal_availability": signal_availability,
            "signal_scores": {
                "groq_model_attribution_review": groq_signal["score"],
                "stylometric_heuristics": stylometric_signal["score"],
                "specificity_context_signal": specificity_signal["score"],
            },
            "signal_summaries": {
                "groq_model_attribution_review": groq_signal["summary"],
                "stylometric_heuristics": stylometric_signal["summary"],
                "specificity_context_signal": specificity_signal["summary"],
            },
            "combined_ai_score": decision["combined_ai_score"],
            "signal_weights": decision["signal_weights"],
            "label": label,
            "transparency_label": label,
            "provenance_certificate": provenance_certificate,
            "creator_verified_human": provenance_certificate is not None,
            "display": display,
            "appeal_filed": False,
            "status": "classified",
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "content_type": submission["content_type"],
            "content_summary": submission["content_summary"],
            "attribution": attribution,
            "confidence": confidence,
            "label": label,
            "transparency_label": label,
            "provenance_certificate": provenance_certificate,
            "display": display,
            "groq_signal_available": groq_signal_available,
            "signal_availability": signal_availability,
            "signals": signals,
        }
    )


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({"error": "Request body must be JSON."}), 400

    content_id = data.get("content_id")
    creator_reasoning = data.get("creator_reasoning")
    creator_id = data.get("creator_id")

    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "A non-empty content_id field is required."}), 400

    if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return jsonify(
            {"error": "A non-empty creator_reasoning field is required."}
        ), 400

    original_decision = find_latest_attribution_decision(content_id)
    if original_decision is None:
        return jsonify({"error": "content_id was not found."}), 404

    appeal_id = str(uuid.uuid4())
    created_at = utc_timestamp()
    appeal_creator_id = creator_id or original_decision.get("creator_id")

    log_event(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "appeal_submitted",
            "appeal_id": appeal_id,
            "content_id": content_id,
            "creator_id": appeal_creator_id,
            "creator_reasoning": creator_reasoning,
            "appeal_reasoning": creator_reasoning,
            "original_attribution": original_decision.get("attribution"),
            "original_confidence": original_decision.get("confidence"),
            "original_signal_scores": original_decision.get(
                "signal_scores",
                {
                    "groq_model_attribution_review": original_decision.get(
                        "llm_score"
                    ),
                    "stylometric_heuristics": original_decision.get(
                        "stylometric_score"
                    ),
                    "specificity_context_signal": original_decision.get(
                        "specificity_context_score"
                    ),
                },
            ),
            "original_signal_summaries": original_decision.get(
                "signal_summaries", {}
            ),
            "appeal_filed": True,
            "status": "under_review",
            "created_at": created_at,
        }
    )

    return jsonify(
        {
            "appeal_id": appeal_id,
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received. The original classification is now under review.",
            "created_at": created_at,
        }
    ), 201


@app.route("/log", methods=["GET"])
def view_log():
    return jsonify({"entries": get_log()})


@app.route("/analytics.json", methods=["GET"])
def analytics_json():
    return jsonify(analytics_summary())


@app.route("/analytics", methods=["GET"])
def analytics_dashboard():
    summary = analytics_summary()
    patterns = summary["detection_patterns"]
    rows = "\n".join(
        (
            "<tr>"
            f"<td>{escape(attribution)}</td>"
            f"<td>{count}</td>"
            "</tr>"
        )
        for attribution, count in patterns.items()
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Provenance Guard Analytics</title>
  <style>
    body {{
      color: #1f2933;
      font-family: Arial, sans-serif;
      margin: 32px;
      max-width: 900px;
    }}
    h1, h2 {{
      margin-bottom: 8px;
    }}
    .metrics {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin: 20px 0;
    }}
    .metric {{
      border: 1px solid #ccd5df;
      border-radius: 6px;
      padding: 14px;
    }}
    .metric strong {{
      display: block;
      font-size: 28px;
      margin-top: 8px;
    }}
    table {{
      border-collapse: collapse;
      margin-top: 12px;
      width: 100%;
    }}
    th, td {{
      border: 1px solid #ccd5df;
      padding: 10px;
      text-align: left;
    }}
    th {{
      background: #eef3f8;
    }}
  </style>
</head>
<body>
  <h1>Provenance Guard Analytics</h1>
  <p>Read-only dashboard generated from the structured audit log.</p>

  <section class="metrics">
    <div class="metric">Classification decisions<strong>{summary["decision_count"]}</strong></div>
    <div class="metric">Appeals filed<strong>{summary["appeal_count"]}</strong></div>
    <div class="metric">Appeal rate<strong>{summary["appeal_rate_percent"]}%</strong></div>
    <div class="metric">Average confidence<strong>{summary["average_confidence"]}</strong></div>
    <div class="metric">Verified creator submissions<strong>{summary["verified_creator_decisions"]}</strong></div>
  </section>

  <h2>Detection Patterns</h2>
  <table>
    <thead>
      <tr><th>Attribution</th><th>Count</th></tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</body>
</html>"""
    return html


if __name__ == "__main__":
    app.run(port=5000, debug=True)

import json
import os
import re
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq


load_dotenv()

app = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

LOG_PATH = "audit_log.jsonl"
GROQ_MODEL = "llama-3.3-70b-versatile"
CERTIFICATE_DURATION_DAYS = 90
VERIFIED_HUMAN_DISPLAY_TEXT = "Verified human creator"
SUPPORTED_CONTENT_TYPES = {"text", "image_description"}
FORMULAIC_PHRASES = [
    "it is important to note",
    "it is equally essential",
    "furthermore",
    "stakeholders",
    "various sectors",
    "responsible deployment",
    "transformative",
    "paradigm shift",
    "ethical implications",
]
SPECIFIC_CONTEXT_WORDS = {
    "apartment",
    "broth",
    "coffee",
    "downtown",
    "friend",
    "home",
    "kitchen",
    "mom",
    "neighborhood",
    "porch",
    "ramen",
    "room",
    "street",
    "thirsty",
    "today",
    "tonight",
    "yesterday",
}
SENSORY_WORDS = {
    "amber",
    "bitter",
    "bright",
    "cold",
    "hot",
    "loud",
    "quiet",
    "rose",
    "salty",
    "soft",
    "spicy",
    "sweet",
    "warm",
}
ABSTRACT_GENERIC_WORDS = {
    "benefits",
    "collaborate",
    "deployment",
    "ethical",
    "fundamental",
    "implications",
    "numerous",
    "paradigm",
    "responsible",
    "sectors",
    "society",
    "stakeholders",
    "transformative",
    "various",
}
FIRST_PERSON_WORDS = {"i", "me", "my", "mine", "we", "us", "our", "ours"}
SIGNAL_WEIGHTS = {
    "groq_model_attribution_review": 0.50,
    "stylometric_heuristics": 0.30,
    "specificity_context_signal": 0.20,
}


def clamp_score(score):
    return max(0.0, min(1.0, float(score)))


def score_feature(value, human_like, ai_like):
    if human_like == ai_like:
        return 0.5

    if human_like < ai_like:
        return clamp_score((value - human_like) / (ai_like - human_like))

    return clamp_score((human_like - value) / (human_like - ai_like))


def utc_timestamp(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def attribution_from_combined_score(score):
    if score >= 0.80:
        return "likely_ai"
    if score <= 0.30:
        return "likely_human"
    return "uncertain"


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


def stringify_metadata_value(value):
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(
            str(item)
            for item in value
            if isinstance(item, (str, int, float, bool))
        )
    return json.dumps(value, sort_keys=True)


def normalize_submission_payload(data):
    content_type = data.get("content_type", "text")
    if not isinstance(content_type, str):
        raise ValueError("content_type must be a string when supplied.")

    content_type = content_type.strip().lower()
    if content_type not in SUPPORTED_CONTENT_TYPES:
        supported = ", ".join(sorted(SUPPORTED_CONTENT_TYPES))
        raise ValueError(f"content_type must be one of: {supported}.")

    if content_type == "text":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A non-empty text field is required.")

        return {
            "content_type": "text",
            "analysis_text": text.strip(),
            "content_summary": {
                "content_type": "text",
                "text_character_count": len(text.strip()),
            },
        }

    image_description = data.get("image_description")
    if not isinstance(image_description, str) or not image_description.strip():
        raise ValueError(
            "A non-empty image_description field is required for image_description content."
        )

    image_metadata = data.get("image_metadata", {})
    if image_metadata is None:
        image_metadata = {}
    if not isinstance(image_metadata, dict):
        raise ValueError("image_metadata must be a JSON object when supplied.")

    metadata_parts = [
        f"{key}: {stringify_metadata_value(value)}"
        for key, value in sorted(image_metadata.items())
    ]
    analysis_parts = [f"Image description: {image_description.strip()}"]
    if metadata_parts:
        analysis_parts.append("Image metadata: " + "; ".join(metadata_parts))

    return {
        "content_type": "image_description",
        "analysis_text": "\n".join(analysis_parts),
        "content_summary": {
            "content_type": "image_description",
            "description_character_count": len(image_description.strip()),
            "metadata_fields": sorted(str(key) for key in image_metadata),
        },
    }


def unavailable_groq_signal(error):
    return {
        "name": "groq_model_attribution_review",
        "score": 0.5,
        "summary": f"Signal unavailable; using neutral placeholder. Error: {error}",
    }


def stylometric_heuristics(text):
    """
    Return the structural stylometric signal.

    Output shape:
    {
        "name": "stylometric_heuristics",
        "score": 0.0-1.0,  # higher means more structurally AI-like
        "metrics": {
            "sentence_length_variance": ...,
            "type_token_ratio": ...,
            "formulaic_phrase_density": ...,
        },
        "summary": "short explanation"
    }
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string.")

    sentences = [
        sentence.strip()
        for sentence in re.split(r"[.!?]+", text)
        if sentence.strip()
    ]
    words = re.findall(r"[A-Za-z']+", text.lower())
    word_count = len(words)
    sentence_lengths = [
        len(re.findall(r"[A-Za-z']+", sentence.lower()))
        for sentence in sentences
    ]
    unique_words = len(set(words))
    lower_text = text.lower()

    sentence_length_variance = (
        statistics.pvariance(sentence_lengths) if len(sentence_lengths) > 1 else 0.0
    )
    type_token_ratio = unique_words / word_count if word_count else 0.0
    formulaic_phrase_hits = sum(
        1 for phrase in FORMULAIC_PHRASES if phrase in lower_text
    )
    formulaic_phrase_density = formulaic_phrase_hits / max(len(sentences), 1)

    variance_score = score_feature(sentence_length_variance, 40.0, 3.0)
    vocabulary_score = score_feature(type_token_ratio, 0.85, 0.45)
    formulaic_score = clamp_score(formulaic_phrase_density / 1.5)

    if word_count < 25:
        score = 0.5
        summary = (
            "Text is short, so structural evidence is limited and remains neutral."
        )
    else:
        score = clamp_score(
            (variance_score * 0.25)
            + (vocabulary_score * 0.15)
            + (formulaic_score * 0.60)
        )

        if score >= 0.65:
            summary = (
                "Structural patterns are relatively uniform, repetitive, or generic."
            )
        elif score <= 0.35:
            summary = (
                "Structural patterns show varied rhythm, vocabulary, or punctuation."
            )
        else:
            summary = "Structural patterns are mixed and do not strongly lean either way."

    return {
        "name": "stylometric_heuristics",
        "score": round(score, 3),
        "metrics": {
            "word_count": word_count,
            "sentence_count": len(sentences),
            "sentence_length_variance": round(sentence_length_variance, 3),
            "type_token_ratio": round(type_token_ratio, 3),
            "formulaic_phrase_hits": formulaic_phrase_hits,
            "formulaic_phrase_density": round(formulaic_phrase_density, 3),
        },
        "summary": summary,
    }


def specificity_context_signal(text):
    """
    Return the concrete-context signal.

    Output shape:
    {
        "name": "specificity_context_signal",
        "score": 0.0-1.0,  # higher means more low-specificity or abstract AI-like
        "metrics": {
            "specificity_density": ...,
            "abstract_density": ...,
        },
        "summary": "short explanation"
    }
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string.")

    words = re.findall(r"[A-Za-z']+", text.lower())
    word_count = len(words)
    numeric_tokens = re.findall(r"\b\d+(?:[.,]\d+)?\b", text)
    first_person_hits = sum(1 for word in words if word in FIRST_PERSON_WORDS)
    context_hits = sum(1 for word in words if word in SPECIFIC_CONTEXT_WORDS)
    sensory_hits = sum(1 for word in words if word in SENSORY_WORDS)
    abstract_hits = sum(1 for word in words if word in ABSTRACT_GENERIC_WORDS)

    specificity_hits = (
        first_person_hits + context_hits + sensory_hits + len(numeric_tokens)
    )
    specificity_density = specificity_hits / word_count if word_count else 0.0
    abstract_density = abstract_hits / word_count if word_count else 0.0

    if word_count < 25:
        score = 0.5
        summary = (
            "Text is short, so concrete-context evidence is limited and remains neutral."
        )
    else:
        low_specificity_score = score_feature(specificity_density, 0.12, 0.01)
        abstract_language_score = score_feature(abstract_density, 0.02, 0.12)
        score = clamp_score(
            (low_specificity_score * 0.65)
            + (abstract_language_score * 0.35)
        )

        if score >= 0.65:
            summary = "Text has limited concrete context or leans abstract/generic."
        elif score <= 0.35:
            summary = "Text includes concrete personal, sensory, or situational details."
        else:
            summary = "Concrete-context evidence is mixed."

    return {
        "name": "specificity_context_signal",
        "score": round(score, 3),
        "metrics": {
            "word_count": word_count,
            "first_person_hits": first_person_hits,
            "context_word_hits": context_hits,
            "sensory_word_hits": sensory_hits,
            "numeric_token_hits": len(numeric_tokens),
            "abstract_generic_hits": abstract_hits,
            "specificity_density": round(specificity_density, 3),
            "abstract_density": round(abstract_density, 3),
        },
        "summary": summary,
    }


def combine_signals(*signals):
    """Combine ensemble signal scores using the weights from planning.md."""
    signal_scores = {
        signal["name"]: clamp_score(signal["score"])
        for signal in signals
    }
    total_weight = sum(
        SIGNAL_WEIGHTS.get(name, 0.0)
        for name in signal_scores
    )
    if total_weight <= 0:
        raise ValueError("At least one known signal is required.")

    weighted_score = sum(
        signal_scores[name] * SIGNAL_WEIGHTS.get(name, 0.0)
        for name in signal_scores
    ) / total_weight
    disagreement = max(signal_scores.values()) - min(signal_scores.values())

    if disagreement >= 0.35:
        if weighted_score > 0.50:
            combined_ai_score = max(0.50, weighted_score - 0.10)
        elif weighted_score < 0.50:
            combined_ai_score = min(0.50, weighted_score + 0.10)
        else:
            combined_ai_score = weighted_score
    else:
        combined_ai_score = weighted_score

    combined_ai_score = round(clamp_score(combined_ai_score), 3)

    return {
        "combined_ai_score": combined_ai_score,
        "confidence": combined_ai_score,
        "attribution": attribution_from_combined_score(combined_ai_score),
        "disagreement": round(disagreement, 3),
        "signal_weights": {
            name: SIGNAL_WEIGHTS[name]
            for name in signal_scores
            if name in SIGNAL_WEIGHTS
        },
    }


def build_transparency_label(attribution, confidence):
    """Return one of the exact transparency label variants from planning.md."""
    if attribution == "likely_ai":
        confidence_percent = round(confidence * 100)
        return (
            "Likely AI-generated. Provenance Guard found strong signs of "
            f"AI-generated writing patterns. Confidence: {confidence_percent}%."
        )

    if attribution == "likely_human":
        confidence_percent = round((1 - confidence) * 100)
        return (
            "Likely human-written. Provenance Guard found strong signs of "
            f"human writing patterns. Confidence: {confidence_percent}%."
        )

    confidence_percent = round(confidence * 100)
    return (
        "Origin uncertain. Provenance Guard found mixed signals, so this text "
        "should not be treated as clearly AI-generated or clearly human-written. "
        f"Confidence: {confidence_percent}%."
    )


def groq_model_attribution_review(text):
    """
    Return the first detection signal.

    Output shape:
    {
        "name": "groq_model_attribution_review",
        "score": 0.0-1.0,  # higher means more AI-like
        "summary": "short explanation"
    }
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string.")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set.")

    client = Groq(api_key=api_key, timeout=10.0, max_retries=0)
    prompt = (
        "You are the first detection signal for Provenance Guard. Assess whether "
        "the submitted text reads as AI-generated or human-written based on "
        "semantic and stylistic coherence. Look for generic development, overly "
        "balanced structure, low lived specificity, smooth but formulaic "
        "transitions, and lack of concrete authorial context. Return only valid "
        "JSON with this exact shape: "
        '{"score": 0.0, "summary": "one short sentence"}. '
        "The score must be a number from 0.0 to 1.0, where 1.0 means strongly "
        "AI-like and 0.0 means strongly human-like. Do not claim certainty about "
        "authorship."
    )

    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    raw_content = completion.choices[0].message.content
    assessment = json.loads(raw_content)

    if "score" not in assessment or "summary" not in assessment:
        raise ValueError("Groq response must include score and summary.")

    summary = str(assessment["summary"]).strip()
    if not summary:
        raise ValueError("Groq response summary must not be empty.")

    return {
        "name": "groq_model_attribution_review",
        "score": clamp_score(assessment["score"]),
        "summary": summary,
    }


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

    stylometric_signal = stylometric_heuristics(analysis_text)
    specificity_signal = specificity_context_signal(analysis_text)
    signals = [groq_signal, stylometric_signal, specificity_signal]
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

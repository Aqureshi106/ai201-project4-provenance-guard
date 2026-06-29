import json
import os
import re
import statistics


GROQ_MODEL = "llama-3.3-70b-versatile"
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


def attribution_from_combined_score(score):
    if score >= 0.80:
        return "likely_ai"
    if score <= 0.30:
        return "likely_human"
    return "uncertain"


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
        "available": False,
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

    from groq import Groq

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
        "available": True,
    }

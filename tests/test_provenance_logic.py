import unittest

from provenance_logic import (
    build_transparency_label,
    combine_signals,
    normalize_submission_payload,
    specificity_context_signal,
    stylometric_heuristics,
    unavailable_groq_signal,
)


def signal(name, score):
    return {
        "name": name,
        "score": score,
        "summary": "test signal",
    }


class ProvenanceLogicTests(unittest.TestCase):
    def test_normalize_text_submission_trims_text(self):
        payload = normalize_submission_payload(
            {
                "content_type": " text ",
                "text": "  I wrote this on the porch with coffee.  ",
            }
        )

        self.assertEqual(payload["content_type"], "text")
        self.assertEqual(payload["analysis_text"], "I wrote this on the porch with coffee.")
        self.assertEqual(payload["content_summary"]["text_character_count"], 38)

    def test_normalize_image_description_includes_sorted_metadata(self):
        payload = normalize_submission_payload(
            {
                "content_type": "image_description",
                "image_description": "A wet downtown street at night.",
                "image_metadata": {
                    "source": "creator_upload",
                    "tags": ["street", "night"],
                    "edited": False,
                },
            }
        )

        self.assertEqual(payload["content_type"], "image_description")
        self.assertIn("Image description: A wet downtown street at night.", payload["analysis_text"])
        self.assertIn("edited: False", payload["analysis_text"])
        self.assertIn("source: creator_upload", payload["analysis_text"])
        self.assertIn("tags: street, night", payload["analysis_text"])
        self.assertEqual(
            payload["content_summary"]["metadata_fields"],
            ["edited", "source", "tags"],
        )

    def test_manual_ai_ethics_scores_classify_likely_ai(self):
        decision = combine_signals(
            signal("groq_model_attribution_review", 0.88),
            signal("stylometric_heuristics", 0.787),
            signal("specificity_context_signal", 1.0),
        )

        self.assertEqual(decision["combined_ai_score"], 0.876)
        self.assertEqual(decision["attribution"], "likely_ai")
        self.assertIn(
            "Likely AI-generated",
            build_transparency_label(decision["attribution"], decision["confidence"]),
        )

    def test_manual_mixed_scores_stay_uncertain(self):
        decision = combine_signals(
            signal("groq_model_attribution_review", 0.54),
            signal("stylometric_heuristics", 0.027),
            signal("specificity_context_signal", 0.65),
        )

        self.assertEqual(decision["combined_ai_score"], 0.5)
        self.assertEqual(decision["attribution"], "uncertain")

    def test_ai_ethics_paragraph_produces_ai_like_deterministic_signals(self):
        text = (
            "Artificial intelligence represents a transformative paradigm shift "
            "across various sectors of society. It is important to note that "
            "responsible deployment requires stakeholders to collaborate on "
            "ethical implications, governance practices, and sustainable "
            "benefits. Furthermore, these systems can improve productivity "
            "while raising fundamental questions about accountability."
        )

        stylometric = stylometric_heuristics(text)
        specificity = specificity_context_signal(text)

        self.assertGreaterEqual(stylometric["score"], 0.65)
        self.assertGreaterEqual(specificity["score"], 0.65)

    def test_ramen_paragraph_produces_human_like_deterministic_signals(self):
        text = (
            "I got home late tonight and made spicy ramen in my tiny kitchen. "
            "The broth was too salty at first, so I added hot water and tasted "
            "it again while my friend texted me about tomorrow's class. By the "
            "time I sat down, the apartment was quiet and the bowl was still warm."
        )

        stylometric = stylometric_heuristics(text)
        specificity = specificity_context_signal(text)

        self.assertLessEqual(stylometric["score"], 0.35)
        self.assertLessEqual(specificity["score"], 0.35)

    def test_groq_fallback_is_neutral_but_marked_unavailable(self):
        fallback = unavailable_groq_signal("network timeout")

        self.assertEqual(fallback["name"], "groq_model_attribution_review")
        self.assertEqual(fallback["score"], 0.5)
        self.assertIs(fallback["available"], False)
        self.assertIn("network timeout", fallback["summary"])


if __name__ == "__main__":
    unittest.main()

# Provenance Guard API Contract

This file defines the API surface that the backend implementation must follow. It is intentionally written before implementation so the routes, request bodies, response bodies, statuses, and error shapes stay consistent.

## Shared Rules

- Base path: `/`
- Request and response format: JSON.
- All timestamps use ISO 8601 UTC strings, for example `2026-06-27T20:15:00Z`.
- All confidence and signal scores are numbers from `0.0` to `1.0`.
- Attribution values: `likely_ai`, `likely_human`, `uncertain`.
- Content status values: `classified`, `under_review`.
- Every attribution decision must use both detection signals:
  - `groq_model_attribution_review`
  - `stylometric_heuristics`

## Common Error Response

All errors should return this shape:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Human-readable explanation of what went wrong."
  }
}
```

Common error codes:

| HTTP status | Code | Meaning |
| --- | --- | --- |
| `400` | `invalid_request` | Missing JSON, missing fields, empty text, or invalid field type. |
| `404` | `not_found` | Requested `content_id` or `appeal_id` does not exist. |
| `413` | `content_too_large` | Submitted text exceeds the allowed size. |
| `429` | `rate_limited` | Client exceeded the submission rate limit. |
| `500` | `internal_error` | Unexpected server error. |
| `502` | `signal_unavailable` | A required detection signal failed, so no valid multi-signal decision can be made. |

## `POST /submit`

Submits text for attribution analysis.

### Request Body

```json
{
  "text": "A poem, short story excerpt, blog post, or other text content.",
  "source": "optional platform-provided source or context",
  "creator_id": "optional creator identifier supplied by the host platform"
}
```

Required fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `text` | string | Yes | Must not be empty. |
| `source` | string | No | Optional platform context, such as `blog_post` or `poem`. |
| `creator_id` | string | No | Optional host-platform creator identifier. |

### Success Response

HTTP `201 Created`

```json
{
  "content_id": "cnt_001",
  "status": "classified",
  "attribution": "likely_ai",
  "confidence": 0.95,
  "transparency_label": "Likely AI-generated. Provenance Guard found strong signs of AI-generated writing patterns. Confidence: 95%.",
  "signals": [
    {
      "name": "groq_model_attribution_review",
      "score": 0.88,
      "summary": "The model found polished structure, generic transitions, and low lived specificity."
    },
    {
      "name": "stylometric_heuristics",
      "score": 0.93,
      "summary": "Sentence length variance, vocabulary diversity, punctuation density, and repetition look unusually uniform."
    }
  ],
  "created_at": "2026-06-27T20:15:00Z"
}
```

### Rate Limit

`POST /submit` is limited to **10 requests per minute per client IP**.

### Required Side Effects

- Create a new `content_id`.
- Run both detection signals.
- Generate an attribution result, confidence score, and transparency label.
- Write an `attribution_decision` event to the audit log.

## `GET /content/{content_id}`

Returns the current classification and review status for one submitted content item.

### Path Parameters

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `content_id` | string | Yes | ID returned by `POST /submit`. |

### Success Response

HTTP `200 OK`

```json
{
  "content_id": "cnt_001",
  "status": "under_review",
  "attribution": "likely_ai",
  "confidence": 0.95,
  "transparency_label": "Likely AI-generated. Provenance Guard found strong signs of AI-generated writing patterns. Confidence: 95%.",
  "signals": [
    {
      "name": "groq_model_attribution_review",
      "score": 0.88,
      "summary": "The model found polished structure, generic transitions, and low lived specificity."
    },
    {
      "name": "stylometric_heuristics",
      "score": 0.93,
      "summary": "Sentence length variance, vocabulary diversity, punctuation density, and repetition look unusually uniform."
    }
  ],
  "created_at": "2026-06-27T20:15:00Z",
  "updated_at": "2026-06-27T20:20:00Z"
}
```

### Required Side Effects

- None. This is a read-only endpoint.

## `POST /appeal`

Allows a creator to contest a classification.

### Request Body

```json
{
  "content_id": "cnt_001",
  "creator_reasoning": "This was drafted from my personal journal and edited manually.",
  "creator_id": "optional creator identifier supplied by the host platform"
}
```

Required fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `content_id` | string | Yes | Must match an existing submission. |
| `creator_reasoning` | string | Yes | Creator's explanation for why the classification is wrong. |
| `creator_id` | string | No | Optional host-platform creator identifier. |

### Success Response

HTTP `201 Created`

```json
{
  "appeal_id": "apl_001",
  "content_id": "cnt_001",
  "status": "under_review",
  "message": "Appeal received. The original classification is now under review.",
  "created_at": "2026-06-27T20:20:00Z"
}
```

### Required Side Effects

- Store the creator's appeal reasoning.
- Link the appeal to the original decision.
- Update the content status to `under_review`.
- Write an `appeal_submitted` event to the audit log.
- Do not automatically reclassify the content.

## `GET /log`

Returns structured audit-log events. This endpoint is used for grading evidence and debugging.

### Query Parameters

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `content_id` | string | No | If supplied, return only events for that content item. |
| `event_type` | string | No | Optional filter, such as `attribution_decision` or `appeal_submitted`. |
| `limit` | integer | No | Maximum number of events to return. Default: `50`. |

### Success Response

HTTP `200 OK`

```json
{
  "events": [
    {
      "event_id": "evt_001",
      "event_type": "attribution_decision",
      "content_id": "cnt_001",
      "timestamp": "2026-06-27T20:15:00Z",
      "attribution": "likely_ai",
      "confidence": 0.95,
      "status": "classified",
      "transparency_label": "Likely AI-generated. Provenance Guard found strong signs of AI-generated writing patterns. Confidence: 95%.",
      "signals": [
        {
          "name": "groq_model_attribution_review",
          "score": 0.88,
          "summary": "The model found polished structure, generic transitions, and low lived specificity."
        },
        {
          "name": "stylometric_heuristics",
          "score": 0.93,
          "summary": "Sentence length variance, vocabulary diversity, punctuation density, and repetition look unusually uniform."
        }
      ]
    },
    {
      "event_id": "evt_002",
      "event_type": "appeal_submitted",
      "appeal_id": "apl_001",
      "content_id": "cnt_001",
      "timestamp": "2026-06-27T20:20:00Z",
      "creator_reasoning": "This was drafted from my personal journal and edited manually.",
      "original_attribution": "likely_ai",
      "original_confidence": 0.95,
      "status": "under_review"
    }
  ]
}
```

### Required Side Effects

- None. This is a read-only endpoint.

## `GET /health`

Returns a minimal service health check.

### Success Response

HTTP `200 OK`

```json
{
  "status": "ok",
  "service": "provenance_guard"
}
```

### Required Side Effects

- None. This is a read-only endpoint.

## Transparency Label Variants

The backend must return one of these exact label text patterns from `POST /submit` and `GET /content/{content_id}`:

| Variant | Exact label text |
| --- | --- |
| High-confidence AI | `"Likely AI-generated. Provenance Guard found strong signs of AI-generated writing patterns. Confidence: {confidence_percent}%."` |
| High-confidence human | `"Likely human-written. Provenance Guard found strong signs of human writing patterns. Confidence: {confidence_percent}%."` |
| Uncertain | `"Origin uncertain. Provenance Guard found mixed signals, so this text should not be treated as clearly AI-generated or clearly human-written. Confidence: {confidence_percent}%."` |

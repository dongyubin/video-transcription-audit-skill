# Transcript Correction Policy

## Evidence Order

Use evidence in this order:

1. Clear audio in the disputed clip.
2. Agreement between model families.
3. High-probability words from the primary timestamp model.
4. Established proper nouns that fit the audible syllables.
5. Local sentence context.

Context alone is not enough to claim exact wording.

## Allowed Changes

- Correct an obvious homophone when the audio or independent models support it.
- Normalize product and person names when pronunciation and context both match.
- Repair punctuation, capitalization, spacing, and Latin model names.
- Remove meaningless ASR artifacts, not meaningful spoken repetitions.

## Disallowed Changes

- Rewriting a sentence to make the speaker sound more polished.
- Filling a missing word solely because grammar suggests it.
- Replacing an unusual but consistently recognized phrase.
- Hiding uncertainty.

## Unresolved Format

Use:

```text
[听不清 00:03:14]
```

In SRT, retain the disputed interval's original start and end times. In the audit report, include every candidate and why no candidate was accepted.

## Confidence Labels

- `confirmed`: clear audio or strong independent-model agreement.
- `probable`: one clear model plus matching proper noun/context.
- `unresolved`: conflicting or phonetically incomplete evidence.

Only `confirmed` and carefully justified `probable` corrections may replace the conservative marker.

"""
Vision-based pattern matching using Claude API.
Sends chart batches + setup library examples to Claude vision
and classifies each chart as Actionable / NMS / No Match.
"""
import base64
import json
import os
import glob
from pathlib import Path
from typing import Dict, List, Optional

try:
    from anthropic import Anthropic
except ImportError:
    raise ImportError("pip install anthropic")

from dotenv import load_dotenv

load_dotenv()


def get_client() -> Anthropic:
    """Initialize Anthropic client from env."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key."
        )
    return Anthropic(api_key=api_key)


def encode_image(filepath: str) -> str:
    """Read image file and return base64 string."""
    with open(filepath, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def get_media_type(filepath: str) -> str:
    """Get media type from file extension."""
    ext = Path(filepath).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def load_setup_library(setup_dir: str = "setup_library") -> Dict:
    """
    Load all setup types from the setup library.
    
    Returns dict keyed by setup name, each containing:
        - description: str (markdown)
        - examples: list of image file paths
    """
    setups = {}
    setup_path = Path(setup_dir)

    for setup_folder in sorted(setup_path.iterdir()):
        if not setup_folder.is_dir():
            continue

        name = setup_folder.name
        desc_file = setup_folder / "description.md"
        examples_dir = setup_folder / "examples"

        description = ""
        if desc_file.exists():
            description = desc_file.read_text()

        examples = []
        if examples_dir.exists():
            for ext in ["*.png", "*.jpg", "*.jpeg"]:
                examples.extend(sorted(glob.glob(str(examples_dir / ext))))

        setups[name] = {
            "description": description,
            "examples": examples,
        }
        print(f"  Loaded setup '{name}': {len(examples)} examples")

    return setups


def build_system_prompt(setups: Dict) -> str:
    """Build the system prompt with setup descriptions."""
    parts = [
        "You are an expert swing trade chart analyst. You analyze daily (D1) candlestick "
        "charts and classify them based on a library of curated setup patterns.",
        "",
        "You will be shown:",
        "1. Reference examples of each setup type (labeled)",
        "2. New charts to classify",
        "",
        "For each new chart, classify it as one of:",
        "- **Actionable** \u2014 The setup is forming or ready for entry NOW",
        "- **NMS** (Need More Sideways) \u2014 The pattern is developing but needs more "
        "time/consolidation before it's ready",
        "- **No Match** \u2014 Does not match any setup in the library",
        "",
        "IMPORTANT: Focus on the SHAPE of the pattern, not annotations or drawn lines. "
        "Some reference examples have key levels marked, others don't. Learn the pattern "
        "from the price action and volume behavior itself.",
        "",
        "Some reference examples show the setup pre-entry (still forming), others show "
        "post-entry (after the move). Both illustrate the same pattern at different stages.",
        "",
        "## Setup Library",
        "",
    ]

    for name, setup in setups.items():
        parts.append(f"### {name}")
        parts.append(setup["description"])
        parts.append("")

    parts.extend([
        "## Output Format",
        "",
        "Respond with a JSON array. For each chart analyzed, return:",
        "```json",
        "[",
        "  {",
        '    \"ticker\": \"SYMBOL\",',
        '    \"classification\": \"Actionable\" | \"NMS\" | \"No Match\",',
        '    \"setup_type\": \"3-4db\" | null,',
        '    \"confidence\": 0.0-1.0,',
        '    \"key_level\": \"$XX.XX or null\",',
        '    \"notes\": \"Brief explanation of why this classification\"',
        "  }",
        "]",
        "```",
        "",
        "Be concise in notes. Focus on volume behavior, bounce duration, and proximity to key levels.",
    ])

    return "\n".join(parts)


def build_messages(
    setups: Dict,
    chart_paths: Dict[str, str],
    max_examples: int = 5,
) -> List[Dict]:
    """
    Build the messages array with setup examples and charts to analyze.
    
    Args:
        setups: Setup library dict from load_setup_library()
        chart_paths: Dict mapping ticker -> chart filepath to analyze
        max_examples: Max example images per setup type (to manage token usage)
    """
    content = []

    # Add setup examples as labeled reference images
    for name, setup in setups.items():
        content.append({
            "type": "text",
            "text": f"--- REFERENCE EXAMPLES for setup '{name}' ---"
        })

        examples = setup["examples"][:max_examples]
        for i, ex_path in enumerate(examples, 1):
            content.append({
                "type": "text",
                "text": f"Example {i}/{len(examples)} of '{name}' setup:"
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": get_media_type(ex_path),
                    "data": encode_image(ex_path),
                },
            })

    # Add separator
    content.append({
        "type": "text",
        "text": (
            "--- NEW CHARTS TO ANALYZE ---\n"
            "Classify each of the following charts. "
            "The ticker symbol is shown in the top-left of each chart."
        ),
    })

    # Add charts to analyze
    for ticker, chart_path in chart_paths.items():
        content.append({
            "type": "text",
            "text": f"Analyze this chart ({ticker}):"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": get_media_type(chart_path),
                "data": encode_image(chart_path),
            },
        })

    return [{"role": "user", "content": content}]


def analyze_batch(
    chart_paths: Dict[str, str],
    setup_dir: str = "setup_library",
    max_examples: int = 5,
    model: str = "claude-sonnet-4-20250514",
) -> List[Dict]:
    """
    Send a batch of charts to Claude vision for classification.
    
    Args:
        chart_paths: Dict mapping ticker -> chart image filepath
        setup_dir: Path to setup library directory
        max_examples: Max reference examples per setup type
        model: Claude model to use
    
    Returns:
        List of classification dicts
    """
    client = get_client()

    print(f"\n  Loading setup library from {setup_dir}...")
    setups = load_setup_library(setup_dir)

    if not setups:
        raise ValueError(f"No setups found in {setup_dir}")

    system_prompt = build_system_prompt(setups)
    messages = build_messages(setups, chart_paths, max_examples)

    total_examples = sum(
        min(len(s["examples"]), max_examples) for s in setups.values()
    )
    print(f"  Sending {len(chart_paths)} charts + {total_examples} reference examples to {model}...")

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
    )

    # Parse JSON from response
    raw_text = response.content[0].text
    
    # Try to extract JSON from the response
    try:
        # Look for JSON array in the response
        start = raw_text.index("[")
        end = raw_text.rindex("]") + 1
        results = json.loads(raw_text[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"  Warning: Could not parse JSON from response: {e}")
        print(f"  Raw response:\n{raw_text}")
        results = []

    # Print summary
    for r in results:
        ticker = r.get("ticker", "?")
        cls = r.get("classification", "?")
        conf = r.get("confidence", 0)
        notes = r.get("notes", "")
        emoji = {"Actionable": "\ud83c\udfaf", "NMS": "\u23f3", "No Match": "\u274c"}.get(cls, "?")
        print(f"  {emoji} {ticker}: {cls} ({conf:.0%}) \u2014 {notes}")

    return results


def analyze_all(
    chart_paths: Dict[str, str],
    setup_dir: str = "setup_library",
    batch_size: int = 10,
    max_examples: int = 5,
    model: str = "claude-sonnet-4-20250514",
) -> List[Dict]:
    """
    Analyze all charts, splitting into batches if needed.
    
    Vision API has image limits, so we batch charts while keeping
    the same reference examples in each batch.
    
    Args:
        chart_paths: Dict mapping ticker -> chart filepath
        setup_dir: Path to setup library
        batch_size: Max charts per API call (separate from the reference examples)
        max_examples: Max reference examples per setup type
        model: Claude model to use
    
    Returns:
        Combined list of all classification results
    """
    items = list(chart_paths.items())
    all_results = []

    total_batches = -(-len(items) // batch_size)  # ceiling division
    print(f"\nAnalyzing {len(items)} charts in {total_batches} batch(es)...")

    for i in range(0, len(items), batch_size):
        batch = dict(items[i : i + batch_size])
        batch_num = i // batch_size + 1
        print(f"\n--- Batch {batch_num}/{total_batches} ({len(batch)} charts) ---")

        results = analyze_batch(
            batch,
            setup_dir=setup_dir,
            max_examples=max_examples,
            model=model,
        )
        all_results.extend(results)

    # Final summary
    actionable = [r for r in all_results if r.get("classification") == "Actionable"]
    nms = [r for r in all_results if r.get("classification") == "NMS"]
    no_match = [r for r in all_results if r.get("classification") == "No Match"]

    print(f"\n{'='*50}")
    print(f"RESULTS SUMMARY")
    print(f"{'='*50}")
    print(f"  \ud83c\udfaf Actionable: {len(actionable)}")
    for r in actionable:
        print(f"     {r['ticker']} \u2014 {r.get('notes', '')}")
    print(f"  \u23f3 NMS: {len(nms)}")
    for r in nms:
        print(f"     {r['ticker']} \u2014 {r.get('notes', '')}")
    print(f"  \u274c No Match: {len(no_match)}")
    print(f"{'='*50}")

    return all_results

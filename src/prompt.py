"""
Prompt builder for Claude chat.
Generates the system prompt and instructions to paste alongside
composite grid images for pattern matching.
"""
import os
from pathlib import Path
from typing import Dict


def load_setup_descriptions(setup_dir: str = "setup_library") -> Dict[str, str]:
    """Load setup descriptions from the setup library."""
    setups = {}
    setup_path = Path(setup_dir)

    for folder in sorted(setup_path.iterdir()):
        if not folder.is_dir():
            continue
        desc_file = folder / "description.md"
        if desc_file.exists():
            setups[folder.name] = desc_file.read_text()

    return setups


def build_chat_prompt(setup_dir: str = "setup_library") -> str:
    """
    Build the prompt to paste into Claude chat alongside composite grid images.
    
    Returns:
        Ready-to-paste prompt string
    """
    setups = load_setup_descriptions(setup_dir)

    parts = [
        "I'm going to show you composite grid images of daily (D1) stock charts.",
        "Each chart is labeled with its ticker symbol.",
        "",
        "Classify EVERY chart in each grid as one of:",
        "- **Actionable** — Setup is forming or ready for entry NOW",
        "- **NMS** (Need More Sideways) — Pattern developing but needs more time",
        "- **No Match** — Does not match any setup below",
        "",
        "Focus on the SHAPE of the pattern from price action and volume, not drawn annotations.",
        "",
        "=" * 50,
        "SETUP LIBRARY",
        "=" * 50,
        "",
    ]

    for name, description in setups.items():
        parts.append(description)
        parts.append("")

    parts.extend([
        "=" * 50,
        "OUTPUT FORMAT",
        "=" * 50,
        "",
        "For each chart, respond with:",
        "",
        "TICKER | Classification | Setup Type | Confidence | Key Level | Notes",
        "",
        "Example:",
        "AAPL | Actionable | 3-4db | 85% | $178.50 | 3-day bounce on declining vol stalling at prior support turned resistance",
        "MSFT | NMS | 3-4db | 60% | $410 | Bounce only 2 days old, needs 1-2 more days",
        "TSLA | No Match | — | — | — | Strong uptrend, no pullback pattern",
        "",
        "Be concise. Focus on volume behavior, bounce duration, and proximity to key levels.",
        "",
        "Now analyze all charts in the attached grid image(s):",
    ])

    return "\n".join(parts)


def save_chat_prompt(
    output_path: str = "output/prompt.txt",
    setup_dir: str = "setup_library",
) -> str:
    """
    Build and save the chat prompt to a text file.
    
    Returns:
        Path to saved prompt file
    """
    prompt = build_chat_prompt(setup_dir)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with open(output_path, "w") as f:
        f.write(prompt)

    print(f"  Saved prompt to {output_path}")
    print(f"  Paste this into Claude chat along with your composite grid images.")
    return output_path

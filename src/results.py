"""
Parse Claude vision output into structured screening results.
(Placeholder \u2014 will be built out when vision analysis workflow is defined)
"""
import json
import os
from datetime import datetime
from typing import List


def save_results(
    results: List[dict],
    output_dir: str = "output/results"
) -> str:
    """
    Save screening results to a dated JSON file.
    
    Expected result format per ticker:
    {
        "ticker": "AAPL",
        "bucket": "actionable" | "nms" | "no_match",
        "setup_type": "episodic_pivot" | "vcp" | ...,
        "confidence": 0.85,
        "notes": "Clean consolidation above 10MA, volume drying up"
    }
    """
    os.makedirs(output_dir, exist_ok=True)
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    filepath = os.path.join(output_dir, f"screening_{date_str}.json")
    
    output = {
        "date": date_str,
        "total_screened": len(results),
        "actionable": [r for r in results if r.get("bucket") == "actionable"],
        "nms": [r for r in results if r.get("bucket") == "nms"],
        "no_match": [r for r in results if r.get("bucket") == "no_match"],
        "results": results
    }
    
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\n  Results saved to {filepath}")
    print(f"    Actionable: {len(output['actionable'])}")
    print(f"    NMS: {len(output['nms'])}")
    print(f"    No match: {len(output['no_match'])}")
    
    return filepath

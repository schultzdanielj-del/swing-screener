"""
Organize generated charts into batches for Claude vision analysis.
"""
import math
from typing import List, Dict


def create_batches(
    chart_paths: Dict[str, str],
    batch_size: int = 20
) -> List[Dict[str, str]]:
    """
    Split chart paths into batches for Claude vision upload.
    
    Args:
        chart_paths: Dict mapping ticker -> chart filepath
        batch_size: Max charts per batch
    
    Returns:
        List of dicts, each mapping ticker -> filepath
    """
    items = list(chart_paths.items())
    num_batches = math.ceil(len(items) / batch_size)
    
    batches = []
    for i in range(num_batches):
        start = i * batch_size
        end = start + batch_size
        batch = dict(items[start:end])
        batches.append(batch)
    
    print(f"  Created {len(batches)} batch(es) of up to {batch_size} charts")
    for i, batch in enumerate(batches, 1):
        print(f"    Batch {i}: {len(batch)} charts \u2014 {', '.join(batch.keys())}")
    
    return batches

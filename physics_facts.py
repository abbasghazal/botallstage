"""Offline physics library: 400 concepts and 600 short worked examples."""
import hashlib
import json
import random
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

FACT_COUNT = 1000
LIBRARY_VERSION = 'physics-1000-v1'

@lru_cache(maxsize=1)
def load_facts():
    rows = json.loads((Path(__file__).parent / 'data' / 'physics_facts.json').read_text(encoding='utf-8'))
    if not isinstance(rows, list) or len(rows) != FACT_COUNT:
        raise ValueError('Physics library must contain exactly 1000 facts')
    if [row.get('id') for row in rows] != list(range(1, FACT_COUNT + 1)):
        raise ValueError('Invalid physics fact IDs')
    if any(not isinstance(row.get('text'), str) or not row['text'].strip()
           or not isinstance(row.get('category'), str) or not row['category'].strip() for row in rows):
        raise ValueError('Empty physics fact or category')
    if len({row['text'].strip() for row in rows}) != FACT_COUNT:
        raise ValueError('Duplicate physics facts')
    return tuple(MappingProxyType(row) for row in rows)

@lru_cache(maxsize=128)
def _order(user_id, cycle):
    seed = hashlib.sha256(f'{LIBRARY_VERSION}:{user_id}:{cycle}'.encode()).digest()
    order = list(range(FACT_COUNT))
    random.Random(int.from_bytes(seed, 'big')).shuffle(order)
    return tuple(order)

def fact_for_position(user_id, position):
    if position < 0:
        raise ValueError('Invalid physics position')
    cycle, cursor = divmod(position, FACT_COUNT)
    return load_facts()[_order(int(user_id), cycle)[cursor]]

def format_fact(fact, daily=False):
    heading = '⚛️ معلومة فيزيائية يومية' if daily else '⚛️ معلومة فيزيائية'
    return f"{heading} — {fact['id']}/{FACT_COUNT}\n📚 {fact['category']}\n\n{fact['text']}"

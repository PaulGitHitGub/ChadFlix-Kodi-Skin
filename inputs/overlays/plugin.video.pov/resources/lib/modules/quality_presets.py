"""Pure metadata choices over an already user-filtered source list.

No settings, I/O, probing, resolution or playback. Only indices and allowlisted
presentation strings leave build_presets.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import re

from modules.source_presentation import audio_rank, is_remux_class

_KEYS = ('highest', 'medium', 'lowest')
_LABELS = ('Highest Quality', 'Medium Quality', 'Lowest Quality · 4K')
_PACKS = frozenset(('true', 'season', 'show'))
_MARKUP = re.compile(r'\[/?(?:B|I|COLOR(?: [^\]]+)?)\]', re.I)


def _text(value):
    return _MARKUP.sub('', str(value or '')).strip()


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number <= 1_000_000 else None


def _tags(source):
    return frozenset(part.strip().upper() for part in _text(source.get('extraInfo')).split('|') if part.strip())


def _available(source):
    if not isinstance(source, dict):
        return False
    cache = str(source.get('cache_provider') or '').lower()
    if 'uncached' in cache or 'unchecked' in cache:
        return False
    # Also honor explicit sanitized status when used by offline evidence/tests.
    if source.get('available') is False or source.get('cache_status') in ('unchecked', 'uncached'):
        return False
    if source.get('scrape_provider') == 'aiostreams':
        return source.get('cached') is True or source.get('library') is True or source.get('source') == 'http'
    return True


def _eligible(results):
    rows = [(index, source) for index, source in enumerate(results)
            if _available(source) and str(source.get('quality') or '').strip().upper() in ('4K', '2160P', '2160', 'UHD')]
    # Match dynamic-range preference defensively. Unknown is never inferred SDR.
    # If the caller's filtered pool has no reported HDR/DV, retain its SDR fallback.
    hdr = frozenset(('D/VISION', 'DOLBY VISION', 'DV', 'HDR', 'HDR10', 'HDR10+'))
    if any(_tags(source).intersection(hdr) for _, source in rows):
        rows = [(index, source) for index, source in rows
                if 'SDR' not in _tags(source) or _tags(source).intersection(hdr)]
    return rows


def _identity(source):
    try:
        return json.dumps(source, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None


def _size(source):
    for field in ('per_file_size_gb', 'file_size_gb', 'episode_size_gb'):
        value = _positive(source.get(field))
        if value is not None:
            return value, 'per_file'
    pack = str(source.get('package') or '').lower() in _PACKS
    explicit = str(source.get('size_scope') or '').lower() in ('file', 'episode', 'per_file', 'single_file')
    # true_size only suppresses the provider's pack division; it does not by
    # itself establish selected-file bytes across all providers.
    if pack and not explicit:
        return None, 'pack_file_unknown'
    value = _positive(source.get('size'))
    if value is None:
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(GB|GIB|MB|MIB|TB|TIB)\s*', _text(source.get('size_label')).upper())
        if match:
            value = _positive(match.group(1))
            if value is not None:
                value = _positive(value * (1 / 1024 if match.group(2) in ('MB', 'MIB') else 1024 if match.group(2) in ('TB', 'TIB') else 1))
    return value, ('per_file' if explicit else 'single_file') if value is not None else 'unknown'


def _size_label(value):
    if value is None:
        return 'N/A'
    rounded = Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP)
    return '<1 GB' if rounded == 0 else '%s GB' % rounded


@dataclass(frozen=True)
class _Candidate:
    index: int
    fidelity: int
    video_rank: int
    audio_rank: int
    video: tuple
    audio: tuple
    codec: str
    channels: str
    size: object
    scope: str

    @property
    def rank(self):
        return self.fidelity, self.video_rank + self.audio_rank, self.audio_rank, self.video_rank

    @property
    def profile(self):
        return self.fidelity, self.video, self.audio, self.codec, self.channels


def _candidate(index, source):
    tags = _tags(source)
    release = ' '.join(_text(source.get(field)) for field in ('name', 'display_name', 'name_info')).upper()
    fidelity = (3 if 'REMUX' in tags or re.search(r'\b(?:REMUX|BDREMUX)\b', release)
                else 2 if tags.intersection(('BLURAY', 'BLU-RAY')) or re.search(r'\bBLU[ ._-]?RAY\b', release)
                else 1 if tags.intersection(('WEB', 'WEB-DL', 'WEBRIP')) or re.search(r'\bWEB(?:[ ._-]?(?:DL|RIP))?\b', release)
                else 0)
    dv = bool(tags.intersection(('D/VISION', 'DOLBY VISION', 'DV')))
    hdr = bool(tags.intersection(('HDR', 'HDR10', 'HDR10+')))
    video = tuple(label for condition, label in ((dv, 'Dolby Vision'), (hdr, 'HDR10+' if 'HDR10+' in tags else 'HDR'), ('SDR' in tags and not (dv or hdr), 'SDR')) if condition)
    spatial = bool(tags.intersection(('ATMOS', 'DOLBY ATMOS', 'DTS-X', 'DTS:X')))
    lossless = bool(tags.intersection(('TRUEHD', 'DOLBY TRUEHD', 'DTS-HD MA', 'DTS-HD MASTER AUDIO', 'FLAC', 'PCM', 'LPCM')))
    audio = []
    if tags.intersection(('TRUEHD', 'DOLBY TRUEHD')):
        audio.append('TrueHD Atmos' if tags.intersection(('ATMOS', 'DOLBY ATMOS')) else 'TrueHD')
    elif tags.intersection(('ATMOS', 'DOLBY ATMOS')):
        audio.append('Dolby Atmos')
    dts = next((tag for tag in ('DTS-X', 'DTS:X', 'DTS-HD MA', 'DTS-HD MASTER AUDIO', 'DTS-HD', 'DTS') if tag in tags), None)
    if dts:
        audio.append('DTS:X' if dts in ('DTS-X', 'DTS:X') else 'DTS-HD MA' if dts == 'DTS-HD MASTER AUDIO' else dts)
    if not audio:
        token = next((tag for tag in ('FLAC', 'LPCM', 'PCM', 'DD+', 'DD-EX', 'DD', 'AAC', 'MP3') if tag in tags), None)
        if token:
            audio.append({'DD+': 'Dolby Digital+', 'DD-EX': 'Dolby Digital EX', 'DD': 'Dolby Digital'}.get(token, token))
    codec = next((label for names, label in ((('AV1',), 'AV1'), (('HEVC', 'HEVC (X265)', 'X265'), 'HEVC'), (('AVC', 'H264', 'H.264'), 'AVC')) if tags.intersection(names)), '')
    channels = next((label for tag, label in (('8CH', '7.1'), ('7CH', '6.1'), ('6CH', '5.1'), ('2CH', 'Stereo')) if tag in tags), '')
    size, scope = _size(source)
    # R10.3: one audio scale for movies and episodes (source_presentation.audio_rank)
    return _Candidate(index, fidelity, int(dv or hdr), audio_rank(tags),
                      video, tuple(audio), codec, channels, size, scope)


def _candidates(results):
    candidates, seen = [], set()
    for index, source in _eligible(results):
        if is_remux_class(source):
            continue                # R10.3: REMUX-class is only ever the fallback, never a menu tier
        identity = _identity(source)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        candidates.append(_candidate(index, source))
    return candidates


def _highest(candidates):
    # R10.3 (owner, 13 Nov): candidates are encodes only (REMUX-class left out in
    # _candidates). Picture first: Dolby Vision > HDR10/HDR10+ > SDR/unknown.
    def picture(candidate):
        return candidate.video_rank + ('Dolby Vision' in candidate.video)
    video = max(picture(candidate) for candidate in candidates)
    band = [candidate for candidate in candidates if picture(candidate) == video]
    # Missing audio is uncertainty, not evidence of inferior audio. Keep unknown
    # audio on the frontier with the strongest known audio within this band.
    audio = max(candidate.audio_rank for candidate in band)
    frontier = [candidate for candidate in band if not candidate.audio or candidate.audio_rank == audio]
    sized = [candidate for candidate in frontier if candidate.size is not None]
    strongest_sized_audio = max((candidate.audio_rank for candidate in sized), default=-1)
    if sized and any(candidate.size is None and candidate.audio_rank > strongest_sized_audio for candidate in frontier):
        return max(frontier, key=lambda candidate: candidate.rank)
    if sized and len({candidate.codec for candidate in sized}) == 1 and sized[0].codec:
        largest = max(candidate.size for candidate in sized)
        near_size = [candidate for candidate in sized if candidate.size >= largest * .90]
        # A sub-10% size gain does not displace stronger known audio metadata.
        return max(near_size, key=lambda candidate: (candidate.audio_rank, candidate.size, -candidate.index))
    # Without a common codec, file size is not comparable quality evidence.
    # Prefer the strongest known metadata, preserving supplied order on ties.
    return max(sized or frontier, key=lambda candidate: candidate.rank)


def _preset(key, candidate, reason):
    result = dict(key=key, label=_LABELS[_KEYS.index(key)], available=candidate is not None,
                  original_index=None, source_label='N/A', size_label='N/A', video_text='N/A', audio_text='N/A', reason=reason)
    if candidate is not None:
        missing = [field for field, known in (('source fidelity', candidate.fidelity > 0), ('video format', bool(candidate.video)),
                   ('audio format', bool(candidate.audio)), ('file size', candidate.size is not None)) if not known]
        result.update(original_index=candidate.index,
            source_label='4K Blu-Ray' if candidate.fidelity >= 2 else '4K Web' if candidate.fidelity == 1 else '4K source',
            size_label=_size_label(candidate.size), video_text=' · '.join(candidate.video + ((candidate.codec,) if candidate.codec else ())) or 'N/A',
            audio_text=' · '.join(candidate.audio + ((candidate.channels,) if candidate.channels else ())) or 'N/A',
            size_scope=candidate.scope, reported_size_gb=candidate.size,
            metadata_confidence='complete' if not missing else 'partial', missing_metadata=missing)
        if missing:
            result['reason'] += ' Unreported fields remain unknown: ' + ', '.join(missing) + '.'
    return result


def build_presets(results, mediatype, duration_minutes=None):
    """Return exactly three safe presentation dictionaries over original indices.

    Caller applies user filters first. This helper never reads settings or adds
    sources. Ranking uses reported metadata and size proxies, not a perfect joint
    video/audio optimum or an equipment compatibility guarantee.
    """
    if mediatype not in ('movie', 'episode'):
        raise ValueError('mediatype must be movie or episode')
    candidates = _candidates(results)
    if not candidates:
        return [_preset(key, None, 'No available 4K option remains in the filtered source list.') for key in _KEYS]
    high = _highest(candidates)
    sized = [candidate for candidate in candidates if candidate.size is not None]
    low = min(sized, key=lambda candidate: (candidate.size, tuple(-part for part in candidate.rank), candidate.index), default=None)
    medium = None
    if low and high.size is not None and high.size >= low.size * 1.25:
        def comparable(candidate):
            return (_positive(duration_minutes) is not None and bool(candidate.codec)
                    and candidate.codec == low.codec == high.codec
                    and all(item.scope in ('single_file', 'per_file') for item in (candidate, low, high)))
        pool = [candidate for candidate in sized if candidate.index not in (high.index, low.index)
                and low.size * 1.10 < candidate.size < high.size * .90
                and (low.rank < candidate.rank < high.rank or comparable(candidate))]
        if pool:
            midpoint_log = (math.log(high.size) + math.log(low.size)) / 2
            medium = min(pool, key=lambda candidate: (abs(math.log(candidate.size) - midpoint_log), tuple(-part for part in candidate.rank), candidate.index))
    if low and (low.index == high.index or (low.profile == high.profile and high.size is not None and low.size > high.size * .80)):
        low = None
    return [
        _preset('highest', high, 'Reported source fidelity, video and audio metadata; missing audio stays uncertain. Matching-codec size can act as a bitrate proxy. Quality and equipment compatibility are not verified.'),
        _preset('medium', medium, 'Balanced intermediate reported size and metadata; this is a compression/bandwidth heuristic, not measured picture quality.' if medium else 'No materially distinct intermediate 4K option has enough comparable metadata.'),
        _preset('lowest', low, 'Smallest comparable reported 4K file size; it does not prove lowest perceptual quality.' if low else 'No distinct smaller 4K option has a comparable known file size.'),
    ]

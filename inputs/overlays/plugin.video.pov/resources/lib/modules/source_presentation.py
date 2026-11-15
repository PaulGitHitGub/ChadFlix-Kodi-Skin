"""Local presentation of source metadata; no scraping, probing or resolution."""

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


FILTERS = (
    ('all', 'All'), ('dolby_vision', 'Dolby Vision'), ('hdr', 'HDR'),
    ('atmos', 'Atmos'), ('dts', 'DTS'), ('sdr', 'SDR'),
)
_MARKUP = re.compile(r'\[/?(?:B|I|COLOR(?: [^\]]+)?)\]', re.I)
_HASH = re.compile(r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$', re.I)
_PACKS = frozenset(('true', 'season', 'show'))
_HDR_FORMATS = frozenset(('HDR', 'HDR10', 'HDR10+', 'D/VISION', 'DOLBY VISION'))
_BLURAY_ACCENT = {True: 'FF1D4ED8', False: 'FF719EFF'}
_IDENTITY_FIELDS = (
    'filename', 'file_name', 'file_path', 'file', 'language', 'languages',
    'audio_language', 'audio_languages', 'edition', 'version', 'package',
    'season', 'episode', 'episode_start', 'episode_end', 'first_season', 'last_season',
)


def _text(value):
    return _MARKUP.sub('', str(value or '')).strip()


def _tags(source):
    return frozenset(part.strip().upper() for part in _text(source.get('extraInfo')).split('|') if part.strip())


def style_source_label(label, focused=False):
    """Accent Blu-Ray labels, including the 4K prefix for 4K Blu-Ray."""
    if label == '4K Blu-Ray':
        return '[COLOR %s]%s[/COLOR]' % (_BLURAY_ACCENT[bool(focused)], label)
    return label.replace('Blu-Ray', '[COLOR %s]Blu-Ray[/COLOR]' % _BLURAY_ACCENT[bool(focused)])


def source_is_available(source):
    """Use reported cache/direct availability without resolving another link."""
    if not isinstance(source, dict):
        return False
    status = str(source.get('cache_provider') or '').lower()
    if 'uncached' in status or 'unchecked' in status:
        return False
    if source.get('scrape_provider') == 'aiostreams':
        return source.get('cached') is True or source.get('library') is True or source.get('source') == 'http'
    return True


def available_sources(results):
    sources, seen = [], set()
    for source in results:
        if not source_is_available(source):
            continue
        # Identical full records are duplicates. Different accounts, resolvers,
        # files, headers or URLs must remain available as distinct fallbacks.
        try:
            identity = json.dumps(source, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError):
            identity = None
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        sources.append(source)
    return sources


# R10.3 (owner, 13 Nov; review M1): REMUX-class = a remux or a full disc, whether or not the name says so.
#  - by name: REMUX / BDRemux / BD-Remux, BDMV, M2TS, ISO, BD25/50/66/100, COMPLETE (UHD) BLURAY, Disc / Disc1
#  - by size: no encoder tag (x264/x265/AV1/XviD) and at least 45 GB at 4K or 22 GB at 1080p. Why these numbers: UHD
#    remuxes run 50-90 GB for a feature (about 55-80 Mbps) while 4K encodes are 10-35 GB; 1080p remuxes 20-40 GB, 1080p
#    encodes 4-15 GB. Absolute, not per hour: rank_playback_sources gets no runtime, and the movie page and Play must
#    use one rule. A 50 GB "x265" release stays an encode (encoder named); episodes rely on the name rules.
_REMUX_NAME = re.compile(r'(?<![a-z0-9])(?:bd[ ._-]?remux|remux|bdmv|m2ts|iso|bd(?:25|50|66|100)'
                         r'|complete[ ._-]+(?:uhd[ ._-]+)?blu[ ._-]?ray|disc[ ._-]?\d{0,2})(?![a-z0-9])', re.I)
_ENCODER = re.compile(r'(?<![a-z0-9])(?:x[ ._-]?26[45]|av1|xvid)(?![a-z0-9])', re.I)
_REMUX_GB = {'4K': 45.0, '1080p': 22.0}
_LOSSLESS = frozenset(('TRUEHD', 'DOLBY TRUEHD', 'DTS-HD MA', 'DTS-HD MASTER AUDIO', 'FLAC', 'PCM', 'LPCM'))
_SPATIAL = frozenset(('ATMOS', 'DOLBY ATMOS', 'DTS-X', 'DTS:X'))
_KNOWN_AUDIO = frozenset(('DTS-HD', 'DTS', 'DD+', 'DD-EX', 'DD', 'AAC', 'MP3'))


def is_remux_class(source):
    tags = _tags(source)
    if 'REMUX' in tags:
        return True
    name = ' '.join(_text(source.get(key)) for key in ('name', 'display_name', 'name_info', 'filename', 'file_name'))
    if _REMUX_NAME.search(name):
        return True
    limit = _REMUX_GB.get(_text(source.get('quality')))
    try:
        size = float(source.get('size') or 0)
    except (TypeError, ValueError):
        size = 0
    return bool(limit) and size >= limit and not _ENCODER.search(name)


def audio_rank(tags):
    """R10.3: one audio scale for movies (quality_presets) and episodes: lossless+spatial 5, lossless 4, spatial 3,
    other known 1, unknown 0."""
    lossless, spatial = bool(tags & _LOSSLESS), bool(tags & _SPATIAL)
    return 4 + int(spatial) if lossless else 3 if spatial else 1 if tags & _KNOWN_AUDIO else 0


def rank_playback_sources(results, resolution_ranks):
    """Order: resolution, encodes before REMUX-class (R10.3), picture (DV > HDR10/HDR10+ > SDR), audio, release."""
    def priority(source):
        tags = _tags(source)
        dynamic_range = (
            int(bool(tags & _HDR_FORMATS))
            + int(bool(tags & {'D/VISION', 'DOLBY VISION'}))
        )
        release = next((rank for family, rank in (
            ({'BLURAY', 'BLU-RAY'}, 2), ({'WEB', 'WEB-DL'}, 1)
        ) if tags & family), 0)
        return (resolution_ranks.get(source.get('quality'), 99),
                int(is_remux_class(source)), -dynamic_range, -audio_rank(tags), -release)

    return sorted(results, key=priority)


def prefer_dynamic_range(results):
    """Hide explicitly SDR-only entries if an eligible HDR/Dolby Vision option remains."""
    tagged = [(source, _tags(source)) for source in results]
    if not any(source_is_available(source) and tags & _HDR_FORMATS for source, tags in tagged):
        return results
    return [source for source, tags in tagged if 'SDR' not in tags or tags & _HDR_FORMATS]


def format_source_size(source):
    """Round the display label; never change the size used by source filters."""
    def positive(value):
        if isinstance(value, bool):
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return number if number.is_finite() and 0 < number <= 1000000 else None

    gb = positive(source.get('size'))
    if gb is None:
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(GB|GIB|MB|MIB|TB|TIB)\s*',
                             _text(source.get('size_label')).upper())
        if match:
            value, unit = positive(match.group(1)), match.group(2)
            if value is not None:
                gb = value / 1024 if unit in ('MB', 'MIB') else value * 1024 if unit in ('TB', 'TIB') else value
    if gb is None or not 0 < gb <= 1000000:
        return 'Size unknown'
    if gb < 1:
        mb = gb * 1024
        return '<1 MB' if mb < 1 else '%s MB' % mb.to_integral_value(rounding=ROUND_HALF_UP)
    return '%s GB' % gb.to_integral_value(rounding=ROUND_HALF_UP)


def _identity(source, tags):
    """Only merge compatible, explicitly identified single-release records."""
    # A torrent pack hash does not identify the episode/file that will resolve.
    if str(source.get('package', '')).lower() in _PACKS:
        return None
    name = (source.get('filename') or source.get('file_name') or source.get('file_path')
            or source.get('name') or source.get('display_name') or '')
    name = ' '.join(_text(name).casefold().split())
    if not name:
        return None
    file_hash = str(source.get('hash') or '').strip()
    if _HASH.fullmatch(file_hash):
        content = ('hash', file_hash.lower())
    else:
        # URL path and query case can be significant. Never rewrite or strip them.
        url = source.get('url') or source.get('url_dl') or source.get('id')
        if not isinstance(url, str) or not url.startswith(('https://', 'http://', 'magnet:')):
            return None
        content = ('url', url)
    explicit = tuple(
        (key, json.dumps(source[key], sort_keys=True, ensure_ascii=False, default=str))
        for key in _IDENTITY_FIELDS if key in source
    )
    # AIOStreams' name_info can contain only formats; retain each actual release
    # filename as identity rather than grouping different files by those tags.
    names = tuple(
        (key, ' '.join(_text(source[key]).casefold().split()))
        for key in ('name', 'display_name', 'name_info') if source.get(key)
    )
    return content, name, names, str(source.get('quality') or ''), tuple(sorted(tags)), explicit


def _describe(source, tags):
    formats, video_badges, audio_badges = set(), [], []
    if 'D/VISION' in tags or 'DOLBY VISION' in tags:
        formats.add('dolby_vision')
        video_badges.append('Dolby Vision')
    if 'HDR' in tags or 'HDR10' in tags or 'HDR10+' in tags:
        formats.add('hdr')
        video_badges.append('HDR10+' if 'HDR10+' in tags else 'HDR')
    if 'SDR' in tags:
        formats.add('sdr')
        video_badges.append('SDR')
    if 'ATMOS' in tags:
        formats.add('atmos')
        audio_badges.append('TrueHD Atmos' if 'TRUEHD' in tags else 'Dolby Atmos')
    elif 'TRUEHD' in tags:
        audio_badges.append('TrueHD')
    dts = next((tag for tag in ('DTS-X', 'DTS:X', 'DTS-HD MA', 'DTS-HD', 'DTS') if tag in tags), None)
    if dts:
        formats.add('dts')
        audio_badges.append('DTS:X' if dts in ('DTS-X', 'DTS:X') else dts)
    if not dts and 'ATMOS' not in tags and 'TRUEHD' not in tags:
        audio = next((tag for tag in ('DD+', 'DD-EX', 'DD', 'AAC', 'MP3') if tag in tags), None)
        if audio:
            audio_badges.append({'DD+': 'Dolby Digital+', 'DD-EX': 'Dolby Digital EX', 'DD': 'Dolby Digital'}.get(audio, audio))
    badges = video_badges + audio_badges
    if not badges:
        badges.append('Format unknown')
    codec = next((tag for tag in ('AV1', 'HEVC', 'AVC', 'XVID', 'DIVX') if tag in tags), None)
    channels = next((tag for tag in ('8CH', '7CH', '6CH', '2CH') if tag in tags), None)
    channel_label = {'8CH': '7.1', '7CH': '6.1', '6CH': '5.1', '2CH': 'Stereo'}.get(channels)
    detail = ' · '.join(value for value in (codec, channel_label) if value)
    quality = _text(source.get('quality')) or 'Unknown quality'
    if 'REMUX' in tags or 'BLURAY' in tags or 'BLU-RAY' in tags:
        label = quality + ' Blu-Ray'
    elif 'WEB' in tags:
        label = quality + ' Web'
    else:
        label = quality + ' source'
    return (label, detail, tuple(badges[:6]), frozenset(formats),
            tuple(video_badges), tuple(audio_badges), codec or '', channel_label or '')


def build_source_groups(results):
    """Build ordered display groups while retaining every original playback source."""
    groups, by_identity = [], {}
    for source in prefer_dynamic_range(available_sources(results)):
        tags = _tags(source)
        identity = _identity(source, tags)
        group = by_identity.get(identity) if identity is not None else None
        if group is None:
            label, detail, badges, formats, video_badges, audio_badges, video_detail, audio_detail = _describe(source, tags)
            group = {
                'source': source, 'alternates': [source], 'badges': badges, 'formats': formats,
                'label': label, 'detail': detail,
                'video_badges': video_badges, 'audio_badges': audio_badges,
                'video_detail': video_detail, 'audio_detail': audio_detail,
                'release_name': _text(source.get('display_name') or source.get('name') or source.get('name_info')),
                'link_count': 1,
            }
            groups.append(group)
            if identity is not None:
                by_identity[identity] = group
        else:
            group['alternates'].append(source)
            group['link_count'] += 1
    for group in groups:
        group['alternates'] = tuple(group['alternates'])
    return groups


def play_facts(source):
    """R10-playback: what the play loader shows for the source being opened (skin Includes_Netflix_Playback.xml):
    (resolution, picture line, sound line), e.g. ('4K', 'Dolby Vision · HEVC · Blu-Ray', 'TrueHD Atmos 7.1 · 61 GB').
    Unknown parts are left out, never guessed; SDR is not named. The page's check publishes the Highest tier's facts
    with this function, and POV publishes the same for each source it opens, so a movie's pre-filled facts and
    POV's are the same text."""
    if not isinstance(source, dict):
        return '', '', ''
    tags = _tags(source)
    _, _, _, _, video_badges, audio_badges, codec, channels = _describe(source, tags)
    picture = next((badge for badge in ('Dolby Vision', 'HDR10+', 'HDR') if badge in video_badges), '')
    if 'REMUX' in tags or 'BLURAY' in tags or 'BLU-RAY' in tags:
        kind = 'Blu-Ray'
    elif 'WEB' in tags or 'WEB-DL' in tags:
        kind = 'Web'
    else:
        kind = ''
    sound = ' '.join(filter(None, (next(iter(audio_badges), ''), channels)))
    size = format_source_size(source)
    res = _text(source.get('quality'))
    return (res if res in ('4K', '1080p', '720p', 'SD') else res[:8],
            ' · '.join(filter(None, (picture, codec, kind))),
            ' · '.join(filter(None, (sound, '' if size == 'Size unknown' else size))))

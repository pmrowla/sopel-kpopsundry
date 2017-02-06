# -*- coding: utf-8 -*-
# Copyright (c) 2016 Peter Rowlands
"""
Kpopsundry sopel module

Author: Peter Rowlands <peter@pmrowla.com>
"""

from __future__ import (
    unicode_literals,
    absolute_import,
    division,
    print_function
)

from datetime import datetime, timedelta
import random
import re
from sched import scheduler
import time
import xml.etree.ElementTree as ET

from ollehtv import OllehTV
import pytz
from dateutil.parser import parse

from sopel.module import (
    commands,
    example,
    interval,
    priority,
    require_admin,
    rule,
)
from sopel.config import (
    ConfigurationError,
)
from sopel.config.types import (
    StaticSection,
    ValidatedAttribute,
)

from oauthlib.oauth2 import (
    LegacyApplicationClient,
    BackendApplicationClient,
    TokenExpiredError,
)
from requests_oauthlib import OAuth2Session
from requests.exceptions import HTTPError
import requests
from pyshorteners import Shortener


KR_TZ = pytz.timezone('Asia/Seoul')


def short_url(sopel, url):
    key = sopel.config.kpopsundry.google_api_key
    if key:
        googl = Shortener(
            'Google',
            api_key=sopel.config.kpopsundry.google_api_key
        )
        return googl.short(url)
    else:
        return url


def add_remember(sopel, remember, response, update_db=True):
    old_response = sopel.memory['remember'].get(remember)
    sopel.memory['remember'][remember] = response
    if update_db:
        if old_response:
            q = 'UPDATE kps_remember SET response = ? WHERE remember = ?;'
            sopel.db.execute(q, (response, remember))
        else:
            q = 'INSERT INTO kps_remember (remember, response) VALUES (?, ?);'
            sopel.db.execute(q, (remember, response))


def setup_remember(sopel):
    sopel.memory['remember'] = {}
    q = (
        'CREATE TABLE IF NOT EXISTS'
        ' kps_remember(remember TEXT, response TEXT);'
    )
    sopel.db.execute(q)
    q = 'SELECT * FROM kps_remember;'
    cursor = sopel.db.execute(q)
    for (remember, response) in cursor.fetchall():
        add_remember(sopel, remember, response, update_db=False)


@rule(r'^.*$')
@priority('low')
def remember_respond(sopel, trigger):
    if sopel.memory.contains('last_remember') and \
       (trigger.time - sopel.memory['last_remember']) < timedelta(seconds=30):
        return
    matches = []
    for remember in sopel.memory['remember']:
        regex = r'^(.*\s)?(?P<remember>{})(\s.*)?$'.format(remember)
        if re.match(regex, trigger.match.group(0), re.I):
            matches.append(remember)
    if matches:
        sopel.memory['last_remember'] = trigger.time
        sopel.say(sopel.memory['remember'][random.choice(matches)])


@require_admin
@commands('remember', 'r')
@example('.remember <remember>: <response>')
def remember(sopel, trigger):
    """Remember something"""
    if trigger.time < (datetime.utcnow() - timedelta(seconds=15)):
        # if message was sent > 15 seconds ago it's probably channel history
        # replay and we should ignore it
        return
    args = trigger.match.group(2)
    if args and ':' in args:
        (new_trigger, response) = trigger.match.group(2).strip().split(':', 1)
        add_remember(sopel, new_trigger.strip(), response.strip())
        sopel.reply('I will remember that')


@require_admin
@commands('forget', 'f')
@example('.forget <remember>')
def forget(sopel, trigger):
    """Forget something"""
    if trigger.time < (datetime.utcnow() - timedelta(seconds=15)) \
       or trigger.nick == sopel.nick:
        # if message was sent > 15 seconds ago it's probably channel history
        # replay and we should ignore it
        return
    if trigger.match.group(2):
        remember = trigger.match.group(2).strip()
        response = sopel.memory['remember'].get(remember)
        if response:
            del sopel.memory['remember'][remember]
            q = 'DELETE FROM kps_remember WHERE remember = ?;'
            sopel.db.execute(q, (remember,))
            sopel.reply('I will forget that')
        else:
            sopel.reply('I don\'t know about that')


@require_admin
@commands('rlist')
def remember_list(sopel, trigger):
    """List remembers"""
    sopel.reply(', '.join(sopel.memory['remember'].keys()))


class KpopsundrySection(StaticSection):
    ogs_username = ValidatedAttribute('ogs_username')
    ogs_password = ValidatedAttribute('ogs_password')
    ogs_client_id = ValidatedAttribute('ogs_client_id')
    ogs_client_secret = ValidatedAttribute('ogs_client_secret')
    kps_strim_client_id = ValidatedAttribute('kps_strim_client_id')
    kps_strim_client_secret = ValidatedAttribute('kps_strim_client_secret')
    kps_strim_callback_uri = ValidatedAttribute('kps_strim_callback_uri')
    google_api_key = ValidatedAttribute('google_api_key')
    ollehtv_device_id = ValidatedAttribute('ollehtv_device_id')
    ollehtv_svc_pw = ValidatedAttribute('ollehtv_svc_pw')


def configure(config):
    """
    Load kpopsundry config settings

    Example cfg:
    [kpopsundry]
        ogs_username = username
        ogs_password = password
        ogs_client_id = client_id
        ogs_client_secret = client_secret

    """
    config.define_section('kpopsundry', KpopsundrySection)
    config.kpopsundry.configure_setting(
        'ogs_username',
        'OGS username'
    )
    config.kpopsundry.configure_setting(
        'ogs_password',
        'OGS password'
    )
    config.kpopsundry.configure_setting(
        'ogs_client_id',
        'OGS client ID'
    )
    config.kpopsundry.configure_setting(
        'ogs_client_secret',
        'OGS client secret'
    )
    config.kpopsundry.configure_setting(
        'kps_strim_client_id',
        'kps-strim client ID'
    )
    config.kpopsundry.configure_setting(
        'kps_strim_client_secret',
        'kps-strim client secret'
    )
    config.kpopsundry.configure_setting(
        'kps_strim_callback_uri',
        'kps-strim client callback_uri'
    )
    config.kpopsundry.configure_setting(
        'ollehtv_device_id',
        'OllehTV DEVICE_ID',
    )
    config.kpopsundry.configure_setting(
        'ollehtv_svc_pw',
        'OllehTV SVC_PW',
    )


def _kps_oauth(sopel):
    client_id = sopel.config.kpopsundry.kps_strim_client_id
    client = BackendApplicationClient(client_id=client_id)
    return OAuth2Session(
        client=client,
        token=sopel.memory['kps_strim']['token']
    )


def _kps_expired_token(sopel, oauth):
    client_id = sopel.config.kpopsundry.kps_strim_client_id
    client_secret = sopel.config.kpopsundry.kps_strim_client_secret
    token = oauth.fetch_token(
        'https://strim.pmrowla.com/o/token/',
        client_id=client_id,
        client_secret=client_secret,
    )
    sopel.memory['kps_strim']['token'] = token


def kps_strim_get(sopel, url):
    oauth = _kps_oauth(sopel)
    try:
        r = oauth.get(url)
    except TokenExpiredError:
        _kps_expired_token(sopel, oauth)
        r = oauth.get(url)
    r.raise_for_status()
    return r


def kps_strim_post(sopel, url, data=None):
    oauth = _kps_oauth(sopel)
    try:
        r = oauth.post(url, json=data)
    except TokenExpiredError:
        _kps_expired_token(sopel, oauth)
        r = oauth.post(url, json=data)
    r.raise_for_status()
    return r


def kps_strim_put(sopel, url, data):
    oauth = _kps_oauth(sopel)
    try:
        r = oauth.put(url, json=data)
    except TokenExpiredError:
        _kps_expired_token(sopel, oauth)
        r = oauth.put(url, json=data)
    r.raise_for_status()
    return r


def ogs_get(sopel, url):
    def save_token(token):
        sopel.memory['ogs_token'] = token

    ogs_client_id = sopel.config.kpopsundry.ogs_client_id
    ogs_client_secret = sopel.config.kpopsundry.ogs_client_secret
    extra = {'client_id': ogs_client_id, 'client_secret': ogs_client_secret}
    client = OAuth2Session(
        ogs_client_id,
        token=sopel.memory['ogs_token'],
        auto_refresh_url='http://online-go.com/oauth2/access_token',
        auto_refresh_kwargs=extra,
        token_updater=save_token)
    r = client.get(url)
    r.raise_for_status()
    return r


def ogs_display_rank(val):
    if val < 30:
        return '{0} Kyu'.format(30 - val)
    else:
        return '{0} Dan'.format((val - 30) + 1)


def get_ogs_user_api(sopel, user):
    if isinstance(user, int):
        url = 'https://online-go.com/api/v1/players/{0}'.format(user)
    else:
        url = 'https://online-go.com/api/v1/players?username={0}'.format(user)
    try:
        r = ogs_get(sopel, url)
    except HTTPError:
        return 'No such player {0}'.format(user)
    data = r.json()
    if 'count' in data and data['count'] > 0:
        player = data['results'][0]
    elif 'id' in data:
        player = data
    else:
        return 'Could not fetch info for OGS player {0}'.format(user)
    profile_url = 'https://online-go.com/user/view/{0}'.format(
        player['id'])
    msg = '{0} ({1}) | {2}'.format(
        player['username'],
        ogs_display_rank(player['ranking']),
        profile_url)
    return msg


@commands('ogs')
@example('.ogs <username>')
def ogs(sopel, trigger):
    """Fetch details about an online-go.com player"""
    if not trigger.match.group(2):
        nick = trigger.nick
    else:
        nick = trigger.match.group(2).strip()
    sopel.reply(get_ogs_user_api(sopel, nick))


@rule(r'.*online-go.com/user/view/(?P<id>\d+).*')
def get_ogs_user(sopel, trigger):
    """Show information for a given OGS player"""
    sopel.say(get_ogs_user_api(sopel, int(trigger.match.group('id'))))


def get_ogs_game_api(sopel, game):
    url = 'https://online-go.com/api/v1/games/{0}'.format(game)
    try:
        r = ogs_get(sopel, url)
    except HTTPError:
        return 'No such game {0}'.format(game)
    data = r.json()
    game_url = 'https://online-go.com/game/{0}'.format(
        data['id'])
    if data['ranked']:
        ranked = 'Ranked'
    else:
        ranked = 'Unranked'
    black = data['players']['black']
    white = data['players']['white']
    msg = '{0} ({1}) | {2} ({3}) vs {4} ({5}) | {6}'.format(
        data['name'],
        ranked,
        black['username'],
        ogs_display_rank(black['ranking']),
        white['username'],
        ogs_display_rank(white['ranking']),
        game_url)
    return msg


@commands('ogsgame')
@example('.ogsgame <game_id>')
def ogs_game(sopel, trigger):
    """Fetch details about an online-go.com game"""
    if trigger.match.group(2):
        game = trigger.match.group(2).strip()
        sopel.reply(get_ogs_game_api(sopel, game))


def delayed_say(sopel, func, *args):
    sopel.say(func(sopel, *args))


@rule(r'.*online-go.com/game/(?P<id>\d+).*')
def get_ogs_game(sopel, trigger):
    """Show information for a given OGS game"""
    # Delay this request, otherwise the API may return empty
    # fields before the game is fully configured
    sched = sopel.memory['ogs_sched']
    sched.enter(5, 1, delayed_say,
                (sopel, get_ogs_game_api, int(trigger.match.group('id'))))
    sched.run()


def format_timedelta(td):
    strs = []
    if td.days:
        if td.days > 1:
            strs.append('{} days'.format(td.days))
        else:
            strs.append('{} day'.format(td.days))
    (hours, seconds) = divmod(td.seconds, 3600)
    if hours:
        if hours > 1:
            strs.append('{} hours'.format(hours))
        else:
            strs.append('{} hour'.format(hours))
    (minutes, seconds) = divmod(seconds, 60)
    if minutes:
        if minutes > 1:
            strs.append('{} minutes'.format(minutes))
        else:
            strs.append('{} minute'.format(minutes))
    if seconds:
        if seconds > 1:
            strs.append('{} seconds'.format(seconds))
        else:
            strs.append('{} second'.format(seconds))
    return ', '.join(strs)


def _next_strim(sopel):
    msgs = []
    data = kps_strim_get(
        sopel,
        'https://strim.pmrowla.com/api/v1/strims/?format=json'
    ).json()
    if data['count']:
        strim = data['results'][0]
        title = strim.get('title')
        timestamp = parse(strim.get('timestamp'))
        channel_slug = strim.get('channel')
        channel_data = kps_strim_get(
            sopel,
            'https://strim.pmrowla.com/api/v1/channels/{}/?format=json'.format(
                channel_slug
            )
        ).json()
        channel_name = channel_data.get('name')
        slug = strim.get('slug')
        td = timestamp - pytz.utc.localize(datetime.utcnow())
        msgs.append('Next strim in {}'.format(
            format_timedelta(td),
        ))
        msgs.append('{} - {}: {}'.format(
            timestamp.astimezone(KR_TZ).strftime('%Y-%m-%d %H:%M KST'),
            channel_name,
            title,
        ))
        msgs.append(
            short_url(
                sopel,
                'https://strim.pmrowla.com/strims/{}/'.format(slug)
            )
        )
    else:
        msgs.append('No scheduled strims')
    return msgs


def _check_live(sopel, notify=True):
    live = sopel.memory['kps_strim']['live']
    url = 'https://secure.pmrowla.com/live'
    params = {'app': 'strim'}
    r = requests.get(url, params=params)
    r.raise_for_status()
    msgs = []
    root = ET.fromstring(r.text)
    if root.find('.//active') is not None:
        if not live and notify:
            msgs.append('Strim is now live')
            msgs.append(short_url(sopel, 'https://strim.pmrowla.com/'))
        sopel.memory['kps_strim']['live'] = True
    else:
        if live and notify:
            msgs.append('Strim finished')
            msgs.extend(_next_strim(sopel))
        sopel.memory['kps_strim']['live'] = False
    if msgs:
        for chan in sopel.channels:
            sopel.say(' | '.join(msgs), chan)


@interval(60)
def check_live(sopel):
    return _check_live(sopel)


@interval(3600 * 12)
def auto_schedule_strims(sopel):
    fetch_upcoming_tv(sopel)


@commands('strim')
def strim(sopel, trigger):
    """Fetch next strim"""
    _check_live(sopel, notify=False)
    msgs = []
    if sopel.memory['kps_strim']['live']:
        msgs.append('Strim is live')
        msgs.append(short_url(sopel, 'https://strim.pmrowla.com/'))
    else:
        msgs.append('Strim is down')
        msgs.extend(_next_strim(sopel))
    if msgs:
        sopel.say(' | '.join(msgs))


class TVStation(object):

    def __init__(self, name, channel_num):
        self.name = name
        self.channel_num = channel_num


class TVShow(object):

    def __init__(self, name, station, weekday):
        self.name = name
        self.station = station
        self.weekday = weekday


DEFAULT_STATIONS = {
    'mbc-every1': TVStation('MBC Every1', 1),
    'sbs': TVStation('SBS', 5),
    'kbs2': TVStation('KBS2', 7),
    'kbs1': TVStation('KBS1', 9),
    'mbc': TVStation('MBC', 11),
    'jtbc': TVStation('JTBC', 15),
    'tvn': TVStation('tvn', 17),
    'mnet': TVStation('Mnet', 27),
    'sbs-fune': TVStation('SBS FunE', 43),
    'mbc-music': TVStation('MBC Music', 97),
    'arirang-tv': TVStation('Arirang TV', 206),
}


DEFAULT_SHOWS = {
    'theshow': TVShow('더쇼', 'sbs-fune', 1),
    'weekly': TVShow('주간 아이돌', 'mbc-every1', 2),
    'showchamp': TVShow('쇼 챔피언', 'mbc-every1', 2),
    'mka': TVShow('M COUNTDOWN', 'mnet', 3),
    'mubank': TVShow('뮤직뱅크', 'kbs2', 4),
    'mucore': TVShow('쇼! 음악중심', 'mbc', 5),
    'inki': TVShow('SBS 인기가요', 'sbs', 6),
}


def add_tv_station(sopel, shortname, name, channel_num, update_db=True):
    old_station = sopel.memory['tv_stations'].get(shortname)
    sopel.memory['tv_stations'][shortname] = TVStation(name, channel_num)
    if update_db:
        if old_station:
            q = ('UPDATE kps_tv_station SET name = ?, channel_num = ?'
                 ' WHERE shortname = ?;')
            sopel.db.execute(q, (name, channel_num, shortname))
        else:
            q = ('INSERT INTO kps_tv_station (shortname, name, channel_num)'
                 ' VALUES (?, ?, ?);')
            sopel.db.execute(q, (shortname, name, channel_num))


def add_tv_show(sopel, shortname, name, station, weekday, update_db=True):
    if station not in sopel.memory['tv_stations']:
        return None
    old_show = sopel.memory['tv_shows'].get(shortname)
    show = TVShow(name, station, weekday)
    sopel.memory['tv_shows'][shortname] = show
    if update_db:
        if old_show:
            q = ('UPDATE kps_tv_show SET name = ?, station = ?, weekday = ?'
                 ' WHERE shortname = ?;')
            sopel.db.execute(q, (name, station, weekday, shortname))
        else:
            q = ('INSERT INTO kps_tv_show (shortname, name, station, weekday)'
                 ' VALUES (?, ?, ?, ?);')
            sopel.db.execute(q, (shortname, name, station, weekday))
    return show


def setup_tv(sopel):
    sopel.memory['tv_stations'] = {}
    q = (
        'CREATE TABLE IF NOT EXISTS'
        ' kps_tv_station(shortname TEXT, name TEXT, channel_num INTEGER);'
    )
    sopel.db.execute(q)
    q = 'SELECT * FROM kps_tv_station;'
    cursor = sopel.db.execute(q)
    for (shortname, name, channel_num) in cursor.fetchall():
        add_tv_station(sopel, shortname, name, channel_num, update_db=False)
    for k, v in DEFAULT_STATIONS.items():
        if k not in sopel.memory['tv_stations']:
            add_tv_station(sopel, k, v.name, v.channel_num)
    r = kps_strim_get(
        sopel,
        'https://strim.pmrowla.com/api/v1/channels/?format=json'
    )
    data = r.json()
    for c in data['results']:
        if c['slug'] not in sopel.memory['tv_stations']:
            add_tv_station(sopel, c['slug'], c['name'], c['num'])
    sopel.memory['tv_shows'] = {}
    q = (
        'CREATE TABLE IF NOT EXISTS'
        ' kps_tv_show(shortname TEXT, name TEXT, station TEXT,'
        ' weekday INTEGER);'
    )
    sopel.db.execute(q)
    q = 'SELECT * FROM kps_tv_show;'
    cursor = sopel.db.execute(q)
    for (shortname, name, station, weekday) in cursor.fetchall():
        add_tv_show(sopel, shortname, name, station, weekday,
                    update_db=False)
    for k, v in DEFAULT_SHOWS.items():
        if k not in sopel.memory['tv_shows']:
            add_tv_show(sopel, k, v.name, v.station, v.weekday)


def _match_live_show(sopel, show, search_prgm):
    show_channel = sopel.memory['tv_stations'][show.station].channel_num
    prgm_chnl = int(search_prgm.get('CHNL_NO', -1))
    prgm_nm = search_prgm.get('PRGM_NM', '')
    regex = r'{}(\s+\d+부)?(\s?\(\d+회\))?'.format(show.name)
    if prgm_chnl == show_channel and re.search(regex, prgm_nm) \
            and '(재)' not in prgm_nm:
        return True
    return False


def schedule_program_strim(sopel, strim_slug, program):
    title = program.get('PRGM_NM', 'Untitled strim')
    description = ''
    start_time = KR_TZ.localize(
        datetime.strptime(program.get('BROAD_DATE_TM'), '%Y.%m.%d %H:%M')
    )
    tmp_time = datetime.strptime(program.get('FIN_TM'), '%H:%M')
    fin_time = start_time.replace(hour=tmp_time.hour, minute=tmp_time.minute)
    if fin_time < start_time:
        # day rolled over
        fin_time = fin_time + timedelta(days=1)
    duration = fin_time - start_time
    channel_slug = ''
    for slug, c in sopel.memory['tv_stations'].items():
        if c.channel_num == int(program.get('CHNL_NO', -1)):
            channel_slug = slug
            break
    if not channel_slug:
        # TODO: maybe add channel
        return
    strim_data = {
        'channel': channel_slug,
        'title': title,
        'slug': '{}-{}'.format(strim_slug, start_time.strftime('%Y%m%d-%H%M')),
        'description': description,
        'timestamp': start_time.isoformat(),
        'duration': str(duration),
    }
    msgs = []
    exists = True
    try:
        kps_strim_get(
            sopel,
            'https://strim.pmrowla.com/api/v1/strims/{}/?format=json'.format(
                strim_data['slug']
            )
        )
    except HTTPError as e:
        if e.response.status_code == 404:
            # if this strim is not scheduled then add it
            exists = False
        else:
            msgs.append(str(e))
    try:
        if not exists:
            kps_strim_post(
                sopel,
                'https://strim.pmrowla.com/api/v1/strims/',
                strim_data
            )
    except HTTPError as e:
        msgs.append(str(e))
    return msgs


def fetch_upcoming_tv(sopel):
    today = pytz.utc.localize(datetime.utcnow()).astimezone(KR_TZ).weekday()
    tmrw = (today + 1) % 7
    otv = sopel.memory['otv']
    programs = []
    for k, v in sopel.memory['tv_shows'].items():
        if v.weekday == today or v.weekday == tmrw:
            results = otv.search(v.name)
            if int(results.get('SRCH_EPG_CNT', 0)) > 0:
                for program in results['SRCH_EPG_LIST']:
                    if _match_live_show(sopel, v, program):
                        programs.append(program)
                        schedule_program_strim(sopel, k, program)
    return programs


@commands('tvguide', 'tv')
def tvguide(sopel, trigger):
    """List upcoming TV programs"""
    programs = fetch_upcoming_tv(sopel)
    if not programs:
        sopel.reply('Nothing on air today or tomorrow')
    for program in programs:
        msg = '[{}-{} KST] {}: {}'.format(
            program['BROAD_DATE_TM'],
            program['FIN_TM'],
            program['CHNL_NM'],
            program['PRGM_NM'],
        )
        sopel.reply(msg)


@commands('tvlist', 'tvl')
def tvlist(sopel, trigger):
    """List known TV programs"""
    if sopel.memory['tv_shows']:
        msg = 'Aired programs: {}'.format(
            ' '.join(sopel.memory['tv_shows'].keys()))
        sopel.reply(msg)
    else:
        sopel.reply('None')


@commands('tvstations')
def tvstations(sopel, trigger):
    """List known TV stations"""
    if sopel.memory['tv_stations']:
        sopel.reply(' '.join(sopel.memory['tv_stations'].keys()))
    else:
        sopel.reply('None')


@require_admin
@commands('tvadd')
@example('.tvadd <shortname> <station> <weekday> <name>')
def tvadd(sopel, trigger):
    """Add a TV program"""
    args = trigger.match.group(2)
    if args:
        try:
            (shortname, station, weekday, name) = \
                args.strip().split(' ', 3)
        except ValueError:
            sopel.reply('Usage: .tvadd <shortname> <station> <weekday> <name>')
            return
        if station not in sopel.memory['tv_stations']:
            sopel.reply('Unknown TV station: {}'.format(station))
            return
        add_tv_show(sopel, shortname, name, station, int(weekday))
        sopel.reply('Added {}'.format(shortname))
    else:
        sopel.reply('Usage: .tvadd <shortname> <station> <weekday> <name>')


@require_admin
@commands('tvdel')
@example('.tvdel <shortname>')
def tvdel(sopel, trigger):
    """Delete a TV program"""
    args = trigger.match.group(2)
    if args:
        shortname = args.strip()
        if shortname not in sopel.memory['tv_shows']:
            sopel.reply('Unknown TV show: {}'.format(shortname))
            return
        del sopel.memory['tv_shows'][shortname]
        q = 'DELETE FROM kps_tv_show WHERE shortname = ?;'
        sopel.db.execute(q, (shortname,))
        sopel.reply('Removed {}'.format(shortname))
    else:
        sopel.reply('Usage: .tvdel <shortname>')


@commands('tvdetails')
@example('.tvdetails <shortname>')
def tvdetails(sopel, trigger):
    """List specifics for TV program"""
    args = trigger.match.group(2)
    if args:
        shortname = args.strip()
        if shortname not in sopel.memory['tv_shows']:
            sopel.reply('Unknown TV show: {}'.format(shortname))
            return
        show = sopel.memory['tv_shows'][shortname]
        weekday = [
            'Monday',
            'Tuesday',
            'Wednesday',
            'Thursday',
            'Friday',
            'Saturday',
            'Sunday'
        ][show.weekday]
        station = sopel.memory['tv_stations'][show.station]
        msg = '{} {}: Airs {}s KST'.format(station.name, show.name, weekday)
        sopel.reply(msg)
    else:
        sopel.reply('Usage: .tvdetails <shortname>')


def setup(sopel):
    """Setup kpopsundry module"""
    sopel.config.define_section('kpopsundry', KpopsundrySection)
    try:
        ogs_username = sopel.config.kpopsundry.ogs_username
        ogs_password = sopel.config.kpopsundry.ogs_password
        ogs_client_id = sopel.config.kpopsundry.ogs_client_id
        ogs_client_secret = sopel.config.kpopsundry.ogs_client_secret
        oauth = OAuth2Session(
            client=LegacyApplicationClient(client_id=ogs_client_id))
        token = oauth.fetch_token(
            token_url='https://online-go.com/oauth2/access_token',
            username=ogs_username,
            password=ogs_password,
            client_id=ogs_client_id,
            client_secret=ogs_client_secret)
        sopel.memory['ogs_token'] = token
    except:
        raise ConfigurationError('Could not authenticate with OGS')
    try:
        client_id = sopel.config.kpopsundry.kps_strim_client_id
        client_secret = sopel.config.kpopsundry.kps_strim_client_secret
        client = BackendApplicationClient(client_id=client_id)
        oauth = OAuth2Session(client=client)
        token = oauth.fetch_token(
            'https://strim.pmrowla.com/o/token/',
            client_id=client_id,
            client_secret=client_secret,
        )
        sopel.memory['kps_strim'] = {}
        sopel.memory['kps_strim']['token'] = token
    except Exception as e:
        raise ConfigurationError(
            'You must reconfigure the kpopsundry module to obtain '
            'a kps-strim OAuth token: {}'.format(e)
        )
    sopel.memory['ogs_sched'] = scheduler(time.time, time.sleep)
    setup_remember(sopel)
    sopel.memory['kps_strim']['live'] = False
    try:
        device_id = sopel.config.kpopsundry.ollehtv_device_id
        svc_pw = sopel.config.kpopsundry.ollehtv_svc_pw
        otv = OllehTV(device_id, svc_pw)
        otv.validate()
        sopel.memory['otv'] = otv
    except Exception as e:
        raise ConfigurationError('Invalid OllehTV credentials: {}'.format(e))
    setup_tv(sopel)

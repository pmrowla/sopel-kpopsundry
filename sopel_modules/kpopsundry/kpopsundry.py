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
    if trigger.time < (datetime.utcnow() - timedelta(seconds=15)):
        # if message was sent > 15 seconds ago it's probably channel history
        # replay and we should ignore it
        return
    matches = []
    for remember in sopel.memory['remember']:
        regex = r'^(.*\s)?(?P<remember>{})(\s.*)?$'.format(remember)
        if re.match(regex, trigger.match.group(0)):
            matches.append(remember)
    if matches:
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


def kps_strim_get(sopel, url):
    def save_token(token):
        sopel.memory['kps_strim']['token'] = token

    client_id = sopel.config.kpopsundry.kps_strim_client_id
    client = BackendApplicationClient(client_id=client_id)
    oauth = OAuth2Session(
        client=client,
        token=sopel.memory['kps_strim']['token']
    )
    try:
        r = oauth.get(url)
    except TokenExpiredError:
        client_secret = sopel.config.kpopsundry.kps_strim_client_secret
        token = oauth.fetch_token(
            'https://strim.pmrowla.com/o/token/',
            client_id=client_id,
            client_secret=client_secret,
        )
        sopel.memory['kps_strim']['token'] = token
        r = oauth.get(url)
    r.raise_for_status()
    return r


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
        r = kps_strim_get(
            sopel,
            'https://strim.pmrowla.com/api/v1/channels/?format=json'
        )
        data = r.json()
        channels = []
        for c in data['results']:
            channels.append(c['slug'])
        sopel.memory['kps_strim']['channels'] = channels
    except Exception as e:
        raise ConfigurationError(
            'You must reconfigure the kpopsundry module to obtain '
            'a kps-strim OAuth token: {}'.format(e)
        )
    sopel.memory['ogs_sched'] = scheduler(time.time, time.sleep)
    setup_remember(sopel)
    sopel.memory['kps_strim']['live'] = False


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


def _check_live(sopel, notify=True):
    live = sopel.memory['kps_strim']['live']
    url = 'https://secure.pmrowla.com/live'
    params = {'app': 'strim'}
    r = requests.get(url, params=params)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if root.find('.//active') is not None:
        if not live and notify:
            for chan in sopel.channels:
                sopel.say(
                    'Strim is now live | {}'.format(
                        short_url(sopel, 'https://strim.pmrowla.com/'),
                    ),
                    chan
                )
        sopel.memory['kps_strim']['live'] = True
    else:
        sopel.memory['kps_strim']['live'] = False


@interval(60)
def check_live(sopel):
    return _check_live(sopel)


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
        data = kps_strim_get(
            sopel,
            'https://strim.pmrowla.com/api/v1/strims/?format=json'
        ).json()
        if data['count']:
            strim = data['results'][0]
            title = strim.get('title')
            timestamp = parse(strim.get('timestamp'))
            channel_name = strim.get('channel', {}).get('name')
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
    if msgs:
        sopel.say(' | '.join(msgs))

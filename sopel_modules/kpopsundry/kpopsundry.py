# -*- coding: utf-8 -*-
# Copyright (c) 2016 Peter Rowlands
"""
Kpopsundry sopel module

Author: Peter Rowlands <peter@pmrowla.com>
"""

from __future__ import unicode_literals, absolute_import, division

import re
from sched import scheduler
import time

from sopel.module import (
    commands,
    example,
    priority,
    require_admin,
    rule,
)
from sopel.config import ConfigurationError

from oauthlib.oauth2 import LegacyApplicationClient
from requests_oauthlib import OAuth2Session
from requests.exceptions import HTTPError


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
    for remember in sopel.memory['remember']:
        regex = r'^(.*\s)?(?P<remember>{})(\s.*)?$'.format(remember)
        if re.match(regex, trigger.match.group(0)):
            sopel.say(sopel.memory['remember'][remember])


@require_admin
@commands('remember', 'r')
@example('.remember <remember>: <response>')
def remember(sopel, trigger):
    """Remember something"""
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

    if config.option('Configure Online Go (OGS)?', False):
        config.interactive_add('kpopsundry', 'ogs_username', 'Username')
        config.interactive_add('kpopsundry', 'ogs_password',
                               'Application-specific password')
        config.interactive_add('kpopsundry', 'ogs_client_id', 'Client ID')
        config.interactive_add('kpopsundry', 'ogs_client_secret',
                               'Client secret')


def setup(sopel):
    """Setup kpopsundry module"""
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
    sopel.memory['ogs_sched'] = scheduler(time.time, time.sleep)
    setup_remember(sopel)


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

#!/usr/bin/env python
"""
pgoapi - Pokemon Go API
Copyright (c) 2016 tjado <https://github.com/rifqifatih>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.

Author: rifqi <https://github.com/rifqifatih>
"""

import os
import re
import sys
import json
import time
import struct
import random
import logging
import requests
import argparse

from pgoapi import PGoApi
from pgoapi.utilities import f2i, h2f

from google.protobuf.internal import encoder
from geopy.geocoders import GoogleV3
from s2sphere import Cell, CellId, LatLng

from slackclient import SlackClient

log = logging.getLogger(__name__)

previous_spawn = []

def get_cellid(lat, long):
    origin = CellId.from_lat_lng(LatLng.from_degrees(lat, long)).parent(15)
    walk = [origin.id()]

    # 10 before and 10 after
    next = origin.next()
    prev = origin.prev()
    for i in range(10):
        walk.append(prev.id())
        walk.append(next.id())
        next = next.next()
        prev = prev.prev()

    # return ''.join(map(encode, sorted(walk)))
    return origin.id()

def encode(cellid):
    output = []
    encoder._VarintEncoder()(output.append, cellid)
    return ''.join(output)

def init_config():
    parser = argparse.ArgumentParser()
    config_file = "config.json"

    # If config file exists, load variables from json
    load   = {}
    if os.path.isfile(config_file):
        with open(config_file) as data:
            load.update(json.load(data))

    # Read passed in Arguments
    required = lambda x: not x in load
    parser.add_argument("-a", "--auth_service", help="Auth Service ('ptc' or 'google')",
        required=required("auth_service"))
    parser.add_argument("-u", "--username", help="Username", required=required("username"))
    parser.add_argument("-p", "--password", help="Password", required=required("password"))
    parser.add_argument("-s", "--slack_token", help="Slack Token", required=required("slack_token"))
    parser.add_argument("-y", "--latitude", help="Latitude", required=required("latitude"))
    parser.add_argument("-x", "--longitude", help="Longitude", required=required("longitude"))
    parser.add_argument("-d", "--debug", help="Debug Mode", action='store_true')
    parser.add_argument("-t", "--test", help="Only parse the specified location", action='store_true')
    parser.set_defaults(DEBUG=False, TEST=False)
    config = parser.parse_args()

    # Passed in arguments shoud trump
    for key in config.__dict__:
        if key in load and config.__dict__[key] == None:
            config.__dict__[key] = str(load[key])

    if config.auth_service not in ['ptc', 'google']:
      log.error("Invalid Auth service specified! ('ptc' or 'google')")
      return None

    return config

def main():
    # log settings
    # log format
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(module)10s] [%(levelname)5s] %(message)s')
    # log level for http request class
    logging.getLogger("requests").setLevel(logging.WARNING)
    # log level for main pgoapi class
    logging.getLogger("pgoapi").setLevel(logging.INFO)
    # log level for internal pgoapi class
    logging.getLogger("rpc_api").setLevel(logging.INFO)

    config = init_config()
    if not config:
        return

    if config.debug:
        logging.getLogger("requests").setLevel(logging.DEBUG)
        logging.getLogger("pgoapi").setLevel(logging.DEBUG)
        logging.getLogger("rpc_api").setLevel(logging.DEBUG)

    if config.test:
        return

    # instantiate slack client
    slack_client = SlackClient(config.slack_token)

    # instantiate pgoapi
    api = PGoApi()

    # provide player position on the earth
    position = [float(config.latitude), float(config.longitude), 0]
    api.set_position(*position)

    if not api.login(config.auth_service, config.username, config.password):
        return

    # login
    response_dict = api.call()
    print('Response dictionary: \n\r{}'.format(json.dumps(response_dict, indent=2)))

    while True:
        poi = find_poi(api, position[0], position[1])
        notify_slack(slack_client, poi)
        time.sleep(5 * 60)

def find_poi(api, lat, lng):
    poi = {'pokemons': {}, 'forts': []}
    step_size = 0.0002
    step_limit = 15
    coords = generate_spiral(lat, lng, step_size, step_limit)
    timestamp = int(round(time.time() * 1000))
    for coord in coords:
        lat = coord['lat']
        lng = coord['lng']
        api.set_position(lat, lng, 0)

        cellid = get_cellid(lat, lng)
        api.get_map_objects(latitude=f2i(lat), longitude=f2i(lng), since_timestamp_ms=timestamp, cell_id=cellid)

        response_dict = api.call()
        if response_dict['responses']['GET_MAP_OBJECTS']['status'] == 1:
            for map_cell in response_dict['responses']['GET_MAP_OBJECTS']['map_cells']:
                if 'wild_pokemons' in map_cell:
                    for pokemon in map_cell['wild_pokemons']:
                        pokekey = get_key_from_pokemon(pokemon)
                        pokemon['hides_at'] = time.time() + pokemon['time_till_hidden_ms']/1000
                        poi['pokemons'][pokekey] = pokemon

    print('Open this in a browser to see the path the spiral search took:')
    print_gmaps_dbug(coords)
    return poi

def get_key_from_pokemon(pokemon):
    return '{}-{}'.format(pokemon['spawnpoint_id'], pokemon['pokemon_data']['pokemon_id'])

def print_gmaps_dbug(coords):
    url_string = 'http://maps.googleapis.com/maps/api/staticmap?size=400x400&path='
    for coord in coords:
        url_string += '{},{}|'.format(coord['lat'], coord['lng'])
    print(url_string[:-1])

def generate_spiral(starting_lat, starting_lng, step_size, step_limit):
    coords = [{'lat': starting_lat, 'lng': starting_lng}]
    steps,x,y,d,m = 1, 0, 0, 1, 1
    rlow = 0.0
    rhigh = 0.0005

    while steps < step_limit:
        while 2 * x * d < m and steps < step_limit:
            x = x + d
            steps += 1
            lat = x * step_size + starting_lat + random.uniform(rlow, rhigh)
            lng = y * step_size + starting_lng + random.uniform(rlow, rhigh)
            coords.append({'lat': lat, 'lng': lng})
        while 2 * y * d < m and steps < step_limit:
            y = y + d
            steps += 1
            lat = x * step_size + starting_lat + random.uniform(rlow, rhigh)
            lng = y * step_size + starting_lng + random.uniform(rlow, rhigh)
            coords.append({'lat': lat, 'lng': lng})

        d = -1 * d
        m = m + 1
    return coords

def notify_slack(slack_client, poi):
    print('POI dictionary: \n\r{}'.format(json.dumps(poi, indent=2)))
    pokemons = poi["pokemons"]

    for key in pokemons:
        if key not in previous_spawn:
            previous_spawn.append(key)
            pokemon = pokemons[key]
            text = "Pokemon " + str(pokemon["pokemon_data"]["pokemon_id"]) + " spawned!"
            slack_client.api_call("chat.postMessage", channel="@rifqi", 
                text=text, as_user=False)

if __name__ == '__main__':
    main()

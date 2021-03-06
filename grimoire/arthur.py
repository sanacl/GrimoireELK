#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Grimoire Arthur lib.
#
# Copyright (C) 2015 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#   Alvaro del Castillo San Felix <acs@bitergia.com>
#

import inspect
import logging
import requests
import traceback

from datetime import datetime
from dateutil import parser

from grimoire.ocean.conf import ConfOcean
from grimoire.utils import get_elastic
from grimoire.utils import get_connectors, get_connector_from_name

def feed_backend(url, clean, fetch_cache, backend_name, backend_params,
                 es_index=None, es_index_enrich=None, project=None):
    """ Feed Ocean with backend data """

    backend = None
    repo = {}    # repository data to be stored in conf
    repo['backend_name'] = backend_name
    repo['backend_params'] = backend_params

    if es_index:
        clean = False  # don't remove index, it could be shared

    if not get_connector_from_name(backend_name):
        raise RuntimeError("Unknown backend %s" % backend_name)
    connector = get_connector_from_name(backend_name)
    klass = connector[3]  # BackendCmd for the connector

    try:
        backend_cmd = klass(*backend_params)

        backend = backend_cmd.backend
        ocean_backend = connector[1](backend, fetch_cache=fetch_cache, project=project)

        logging.info("Feeding Ocean from %s (%s)", backend_name, backend.origin)

        if not es_index:
            es_index = backend_name + "_" + backend.origin
        elastic_ocean = get_elastic(url, es_index, clean, ocean_backend)

        ocean_backend.set_elastic(elastic_ocean)

        ConfOcean.set_elastic(elastic_ocean)

        repo['repo_update_start'] = datetime.now().isoformat()

        # perceval backends fetch params
        offset = None
        from_date = None
        category = None

        signature = inspect.signature(backend.fetch)

        if 'from_date' in signature.parameters:
            from_date = backend_cmd.from_date

        if 'offset' in signature.parameters:
            offset = backend_cmd.offset

        if 'category' in signature.parameters:
            category = backend_cmd.category

        # from_date param support
        if offset and category:
            ocean_backend.feed(from_offset=offset, category=category)
        elif offset:
            ocean_backend.feed(from_offset=offset)
        elif from_date and from_date.replace(tzinfo=None) != parser.parse("1970-01-01"):
            if category:
                ocean_backend.feed(backend_cmd.from_date, category=category)
            else:
                ocean_backend.feed(backend_cmd.from_date)
        elif category:
            ocean_backend.feed(category=category)
        else:
            ocean_backend.feed()

    except Exception as ex:
        if backend:
            logging.error("Error feeding ocean from %s (%s): %s" %
                          (backend_name, backend.origin, ex))
            # this print makes blackbird fails
            traceback.print_exc()
        else:
            logging.error("Error feeding ocean %s" % ex)

        repo['success'] = False
        repo['error'] = str(ex)
    else:
        repo['success'] = True

    repo['repo_update'] = datetime.now().isoformat()
    repo['index'] = es_index
    repo['index_enrich'] = es_index_enrich
    repo['project'] = project

    if es_index:
        unique_id = es_index+"_"+backend.origin
        ConfOcean.add_repo(unique_id, repo)
    else:
        logging.debug("Repository not added to Ocean because errors.")
        logging.debug(backend_params)

    logging.info("Done %s " % (backend_name))


def get_items_from_uuid(uuid, enrich_backend, ocean_backend):
    """ Get all items that include uuid """

    # logging.debug("Getting items for merged uuid %s "  % (uuid))

    uuid_fields = enrich_backend.get_fields_uuid()

    terms = ""  # all terms with uuids in the enriched item

    for field in uuid_fields:
        terms += """
         {"term": {
           "%s": {
              "value": "%s"
           }
         }}
         """ % (field, uuid)
        terms += ","

    terms = terms[:-1]  # remove last , for last item

    query = """
    {"query": { "bool": { "should": [%s] }}}
    """ % (terms)

    url_search = enrich_backend.elastic.index_url+"/_search"
    url_search +="?size=1000"  # TODO get all items

    r = requests.post(url_search, data=query)

    eitems = r.json()['hits']['hits']

    if len(eitems) == 0:
        # logging.warning("No enriched items found for uuid: %s " % (uuid))
        return []

    items_ids = []

    for eitem in eitems:
        item_id = enrich_backend.get_item_id(eitem)
        # For one item several eitems could be generated
        if item_id not in items_ids:
            items_ids.append(item_id)

    # Time to get the items
    logging.debug ("Items to be renriched for merged uuids: %s" % (",".join(items_ids)))

    url_mget = ocean_backend.elastic.index_url+"/_mget"

    items_ids_query = ""

    for item_id in items_ids:
        items_ids_query += '{"_id" : "%s"}' % (item_id)
        items_ids_query += ","
    items_ids_query = items_ids_query[:-1]  # remove last , for last item

    query = '{"docs" : [%s]}' % (items_ids_query)
    r = requests.post(url_mget, data=query)

    res_items = r.json()['docs']

    items = []
    for res_item in res_items:
        if res_item['found']:
            items.append(res_item["_source"])

    return items

def refresh_projects(enrich_backend):
    logging.debug("Refreshing project field in %s", enrich_backend.elastic.index_url)
    total = 0

    eitems = enrich_backend.fetch()
    for eitem in eitems:
        new_project = enrich_backend.get_item_project(eitem)
        eitem.update(new_project)
        yield eitem
        total += 1

    logging.info("Total eitems refreshed for project field %i", total)

def refresh_identities(enrich_backend):
    logging.debug("Refreshing identities fields from %s", enrich_backend.elastic.index_url)
    total = 0

    for eitem in enrich_backend.fetch():
        roles = None
        try:
            roles = enrich_backend.roles
        except AttributeError:
            pass
        new_identities = enrich_backend.get_item_sh_from_id(eitem, roles)
        eitem.update(new_identities)
        yield eitem
        total += 1

    logging.info("Total eitems refreshed for identities fields %i", total)

def load_identities(ocean_backend, enrich_backend):
    try:
        from grimoire.elk.sortinghat import SortingHat
    except ImportError:
        logging.warning("SortingHat not available.")

    # First we add all new identities to SH
    items_count = 0
    new_identities = []

    for item in ocean_backend:
        items_count += 1
        # Get identities from new items to be added to SortingHat
        identities = enrich_backend.get_identities(item)
        for identity in identities:
            if identity not in new_identities:
                new_identities.append(identity)
        if items_count % 100 == 0:
            logging.debug("Processed %i items identities (%i identities)",
                          items_count, len(new_identities))
    logging.debug("TOTAL ITEMS: %i", items_count)

    logging.info("Total new identities to be checked %i", len(new_identities))

    SortingHat.add_identities(enrich_backend.sh_db, new_identities,
                              enrich_backend.get_connector_name())

    return items_count

def enrich_items(items, enrich_backend, events=False):
    total = 0

    if not events:
        total= enrich_backend.enrich_items(items)
    else:
        total = enrich_backend.enrich_events(items)
    return total

def get_last_enrich(backend_cmd, enrich_backend):
    last_enrich = None

    if backend_cmd:
        backend = backend_cmd.backend

        # Only supported in data retrieved from a perceval backend
        # Always filter by origin to support multi origin indexes

        filter_ = {"name":"origin",
                   "value":backend.origin}
        # Check if backend supports from_date
        signature = inspect.signature(backend.fetch)

        if 'from_date' in signature.parameters:
            if backend_cmd.from_date.replace(tzinfo=None) != parser.parse("1970-01-01"):
                last_enrich = backend_cmd.from_date
            else:
                last_enrich = enrich_backend.get_last_update_from_es(filter_)

        elif 'offset' in signature.parameters:
            if backend_cmd.offset and backend_cmd.offset != 0:
                last_enrich = backend_cmd.offset
            else:
                last_enrich = enrich_backend.get_last_offset_from_es(filter_)
    else:
        last_enrich = enrich_backend.get_last_update_from_es()

    return last_enrich


def get_ocean_backend(backend_cmd, enrich_backend, no_incremental):
    """ Get the ocean backend configured to start from the last enriched date """

    if no_incremental:
        last_enrich = None
    else:
        last_enrich = get_last_enrich(backend_cmd, enrich_backend)

    logging.debug("Last enrichment: %s", last_enrich)

    backend = None

    connector = get_connectors()[enrich_backend.get_connector_name()]

    if backend_cmd:
        backend = backend_cmd.backend
        signature = inspect.signature(backend.fetch)
        if 'from_date' in signature.parameters:
            ocean_backend = connector[1](backend, from_date=last_enrich)
        elif 'offset' in signature.parameters:
            ocean_backend = connector[1](backend, offset=last_enrich)
        else:
            ocean_backend = connector[1](backend)
    else:
        if last_enrich:
            ocean_backend = connector[1](backend, from_date=last_enrich)
        else:
            ocean_backend = connector[1](backend)

    return ocean_backend

def do_studies(enrich_backend, no_incremental=False):
    if no_incremental:
        last_enrich = None
    else:
        last_enrich = get_last_enrich(None, enrich_backend)

    try:
        for study in enrich_backend.studies:
            logging.info("Starting study: %s (from %s)", study, last_enrich)
            study(from_date=last_enrich)
    except Exception as e:
        logging.error("Problem executing study %s", study)
        traceback.print_exc()

def enrich_backend(url, clean, backend_name, backend_params, ocean_index=None,
                   ocean_index_enrich = None,
                   db_projects_map=None, json_projects_map=None,
                   db_sortinghat=None,
                   no_incremental=False, only_identities=False,
                   github_token=None, studies=False, only_studies=False,
                   url_enrich=None, events_enrich=False,
                   db_user=None, db_password=None, db_host=None,
                   do_refresh_projects=False, do_refresh_identities=False):
    """ Enrich Ocean index """


    backend = None
    enrich_index = None

    if ocean_index or ocean_index_enrich:
        clean = False  # don't remove index, it could be shared

    if do_refresh_projects or do_refresh_identities:
        clean = False  # refresh works over the existing enriched items

    if not get_connector_from_name(backend_name):
        raise RuntimeError("Unknown backend %s" % backend_name)
    connector = get_connector_from_name(backend_name)
    klass = connector[3]  # BackendCmd for the connector

    try:
        backend = None
        backend_cmd = None
        if klass:
            # Data is retrieved from Perceval
            backend_cmd = klass(*backend_params)
            backend = backend_cmd.backend

        if ocean_index_enrich:
            enrich_index = ocean_index_enrich
        else:
            if not ocean_index:
                ocean_index = backend_name + "_" + backend.origin
            enrich_index = ocean_index+"_enrich"
        if events_enrich:
            enrich_index += "_events"

        enrich_backend = connector[2](db_sortinghat, db_projects_map, json_projects_map,
                                      db_user, db_password, db_host)
        if url_enrich:
            elastic_enrich = get_elastic(url_enrich, enrich_index, clean, enrich_backend)
        else:
            elastic_enrich = get_elastic(url, enrich_index, clean, enrich_backend)
        enrich_backend.set_elastic(elastic_enrich)
        if github_token and backend_name == "git":
            enrich_backend.set_github_token(github_token)

        ocean_backend = get_ocean_backend(backend_cmd, enrich_backend, no_incremental)

        if only_studies:
            logging.info("Running only studies (no SH and no enrichment)")
            do_studies(enrich_backend, no_incremental)
        elif do_refresh_projects:
            logging.info("Refreshing project field in enriched index")
            field_id = enrich_backend.get_field_unique_id()
            eitems = refresh_projects(enrich_backend)
            enrich_backend.elastic.bulk_upload_sync(eitems, field_id)
        elif do_refresh_identities:
            logging.info("Refreshing identities fields in enriched index")
            field_id = enrich_backend.get_field_unique_id()
            eitems = refresh_identities(enrich_backend)
            enrich_backend.elastic.bulk_upload_sync(eitems, field_id)
        else:
            clean = False  # Don't remove ocean index when enrich
            elastic_ocean = get_elastic(url, ocean_index, clean, ocean_backend)
            ocean_backend.set_elastic(elastic_ocean)

            logging.info("Adding enrichment data to %s", enrich_backend.elastic.index_url)

            if db_sortinghat:
                # FIXME: This step won't be done from enrich in the future
                total_ids = load_identities(ocean_backend, enrich_backend)
                logging.info("Total identities loaded %i ", total_ids)

            if only_identities:
                logging.info("Only SH identities added. Enrich not done!")

            else:
                # Enrichment for the new items once SH update is finished
                if not events_enrich:
                    enrich_count = enrich_items(ocean_backend, enrich_backend)
                    if enrich_count:
                        logging.info("Total items enriched %i ", enrich_count)
                else:
                    enrich_count = enrich_items(ocean_backend, enrich_backend, events=True)
                    if enrich_count:
                        logging.info("Total events enriched %i ", enrich_count)
                if studies:
                    do_studies(enrich_backend)

    except Exception as ex:
        traceback.print_exc()
        if backend:
            logging.error("Error enriching ocean from %s (%s): %s",
                          backend_name, backend.origin, ex)
        else:
            logging.error("Error enriching ocean %s", ex)

    logging.info("Done %s ", backend_name)

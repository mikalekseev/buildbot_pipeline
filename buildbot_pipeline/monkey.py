import json

import sqlalchemy as sa
from twisted.internet import defer

import buildbot.db.builds as db_builds
import buildbot.data.builds as data_builds
import buildbot.data.connector as data_connector
from buildbot.data import resultspec

from buildbot_pipeline.utils import wrapit, nstr


# original getBuildProperties fetches props one-by-one and very slow for pages
# with many builds. Patched version accepts multiple bid and list of props to
# fetch.
@wrapit(db_builds.BuildsConnectorComponent)
def getBuildProperties(orig, self, bid, resultSpec=None, props=None):
    bp_tbl = self.db.model.build_properties
    if isinstance(bid, (list, tuple)):
        if not bid:
            return {}
        w = bp_tbl.c.buildid.in_(bid)
        many = True
    else:
        w = bp_tbl.c.buildid == bid
        many = False

    if props:
        w = w & bp_tbl.c.name.in_(props)

    def thd(conn):
        q = sa.select(
            [bp_tbl.c.name, bp_tbl.c.value, bp_tbl.c.source, bp_tbl.c.buildid],
            whereclause=w)

        if resultSpec is not None:
            data = resultSpec.thd_execute(conn, q, lambda x: x)
        else:
            data = conn.execute(q)

        result = {}
        for row in data.fetchall():
            prop = (json.loads(row.value), row.source)
            try:
                props = result[row.buildid]
            except KeyError:
                props = result[row.buildid] = {}
            props[row.name] = prop

        if not many:
            result = result.get(bid, {})
        return result

    return self.db.pool.do(thd)


# Fetch builds props in one query with only passed names
@wrapit(data_builds.BuildsEndpoint)
@defer.inlineCallbacks
def get(orig, self, resultSpec, kwargs):
    changeid = kwargs.get('changeid')
    if changeid is not None:
        builds = yield self.master.db.builds.getBuildsForChange(changeid)
    else:
        # following returns None if no filter
        # true or false, if there is a complete filter
        builderid = None
        if 'builderid' in kwargs or 'buildername' in kwargs:
            builderid = yield self.getBuilderId(kwargs)
            if builderid is None:
                return []
        complete = resultSpec.popBooleanFilter("complete")
        buildrequestid = resultSpec.popIntegerFilter("buildrequestid")
        resultSpec.fieldMapping = self.fieldMapping
        builds = yield self.master.db.builds.getBuilds(
            builderid=builderid,
            buildrequestid=kwargs.get('buildrequestid', buildrequestid),
            workerid=kwargs.get('workerid'),
            complete=complete,
            resultSpec=resultSpec)

    # returns properties' list
    filters = resultSpec.popProperties()
    fetch_all_props = '*' in filters

    if filters:
        allprops = yield self.master.db.builds.getBuildProperties(
            [it["id"] for it in builds],
            props=None if fetch_all_props else filters)

    buildscol = []
    for b in builds:
        data = yield self.db2data(b)
        if kwargs.get('graphql'):
            # let the graphql engine manage the properties
            del data['properties']
        else:
            if filters:
                props = allprops.get(data["buildid"], {})
                if props:
                    data["properties"] = props

        buildscol.append(data)
    return buildscol


MULTI_IDS_FIELDS = {
    b'bnums': ('__bnum', str),
    b'builderids': ('builderid', int),
    b'buildids': ('buildid', int),
    b'reqids': ('buildrequestid', int),
}

COLUMN_EXPR = {
    '__bnum': lambda cols: cols['builds.builderid'].concat('-').concat(cols['builds.number'])
}


@wrapit(data_connector.DataConnector)
def resultspec_from_jsonapi(orig, self, req_args, *args, **kwargs):
    additional_filters = []
    for arg, (field, cnv) in MULTI_IDS_FIELDS.items():
        if arg in req_args:
            additional_filters.append(
                resultspec.Filter(field, 'in', list(map(cnv, nstr(req_args.pop(arg)[0]).split(',')))))

    rspec = orig(self, req_args, *args, **kwargs)
    rspec.filters.extend(additional_filters)
    return rspec


@wrapit(resultspec.ResultSpec)
def findColumn(orig, self, query, field):
    cols = {str(col): col for col in query.inner_columns}
    if field in COLUMN_EXPR:
        return COLUMN_EXPR[field](cols)
    return cols[self.fieldMapping[field]]

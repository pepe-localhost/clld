from __future__ import unicode_literals
from collections import OrderedDict, defaultdict
import shutil

from zope.interface import implementer
from sqlalchemy.orm import joinedload_all, joinedload
from pycldf import dataset
from pycldf import sources
from clldutils.path import TemporaryDirectory
from csvw.metadata import URITemplate, Column

from clld.web.adapters.download import Download
from clld.db.meta import DBSession
from clld.db.models.common import (
    ValueSet, Value, Parameter, Language, Contribution, Source, Sentence,
    ContributionContributor,
)
from clld.web.util.helpers import text_citation, get_url_template
from clld.interfaces import ICldfConfig


def url_template(req, route, id_name):
    return URITemplate(get_url_template(
        req, route, relative=False, variable_map={'id': id_name}))


def source2source(req, source):
    """Harmonize the different Source implementations in clld and pycldf."""
    bibrecord = source.bibtex()
    fields = OrderedDict({'%s_url' % req.dataset.id: req.resource_url(source)})
    for key, value in bibrecord.items():
        fields[key] = '; '.join(value) if isinstance(value, list) else value
    return sources.Source(
        getattr(bibrecord.genre, 'value', bibrecord.genre) if bibrecord.genre else 'misc',
        source.id,
        **fields)


def iterrefs(obj):
    def _desc(d):
        return '[%s]' % d.replace(';', '.').replace('[', '{').replace(']', '}') \
            if d else ''

    # For backwards compatibility:
    if not hasattr(obj, 'references') and hasattr(obj, 'valueset'):
        obj = obj.valueset

    for r in obj.references:
        if r.source_pk:
            yield r.source_pk, _desc(r.description)


@implementer(ICldfConfig)
class CldfConfig(object):
    module = 'Wordlist'
    __cache__ = defaultdict(dict)

    def custom_schema(self, req, ds):
        return

    def query(self, model):
        if model == Parameter:
            return DBSession.query(Parameter)
        if model == Language:
            return DBSession.query(Language)
        if model == Contribution:
            return DBSession.query(Contribution).options(joinedload_all(
                Contribution.contributor_assocs,
                ContributionContributor.contributor))
        if model == Sentence:
            return DBSession.query(Sentence).options(joinedload(Sentence.language))
        if model == Source:
            return DBSession.query(Source)
        if model == Value:
            return DBSession.query(Value).options(
                joinedload_all(Value.valueset, ValueSet.references),
                joinedload(Value.domainelement))

    def convert(self, model, item, req):
        self.__cache__[model.__name__][item.pk] = item.id
        if model == Parameter:
            return {'ID': item.id, 'Name': item.name, 'Description': item.description}
        if model == Language:
            return {
                'ID': item.id,
                'Name': item.name,
                'Glottocode': item.glottocode,
                'ISO639P3code': item.iso_code,
                'Latitude': item.latitude,
                'Longitude': item.longitude,
            }
        if model == Contribution:
            return {
                'ID': item.id,
                'Name': item.name,
                'Description': item.description,
                'Contributors': item.formatted_contributors(),
            }
        if model == Sentence:
            return {
                'ID': item.id,
                'Language_ID': self.__cache__['Language'][item.language_pk],
                'Primary_Text': item.name,
                'Analyzed_Word': item.analyzed.split('\t') if item.analyzed else [],
                'Gloss': item.gloss.split('\t') if item.gloss else [],
                'Translated_Text': item.description,
                'Comment': item.comment,
            }
        if model == Source:
            return source2source(req, item)
        if model == Value:
            res = {
                'ID': item.id,
                'Language_ID': self.__cache__['Language'][item.valueset.language_pk],
                'Parameter_ID': self.__cache__['Parameter'][item.valueset.parameter_pk],
                'Contribution_ID': self.__cache__['Contribution'][item.valueset.contribution_pk],
                'Value': (item.domainelement.name if item.domainelement else item.name) or '-',
                'Source': [
                    '{0}{1}'.format(self.__cache__['Source'][spk], d) for spk, d in iterrefs(item)],
            }
            if self.module == 'Wordlist':
                res['Form'] = res['Value']
            return res
        return {}  # pragma: no cover

    def custom_tabledata(self, req, tabledata):
        return tabledata


class CldfDownload(Download):
    ext = 'cldf'
    description = "Dataset in CLDF"

    def create(self, req, filename=None, verbose=True, outfile=None):
        cldf_cfg = req.registry.getUtility(ICldfConfig)

        with TemporaryDirectory() as tmpd:
            cls = getattr(dataset, cldf_cfg.module)
            ds = cls.in_dir(tmpd)
            ds.properties['dc:bibliographicCitation '] = text_citation(req, req.dataset)
            ds.properties['dc:publisher'] = '%s, %s' % (
                req.dataset.publisher_name, req.dataset.publisher_place)
            ds.properties['dc:license'] = req.dataset.license
            ds.properties['dc:issued'] = req.dataset.published.isoformat()
            ds.properties['dc:title'] = req.dataset.name
            ds.properties['dc:creator'] = req.dataset.formatted_editors()
            ds.properties['dc:identifier'] = req.resource_url(req.dataset)
            ds.properties['dcat:accessURL'] = req.route_url('download')
            if DBSession.query(Sentence).count():
                ds.add_component('ExampleTable')
            ds.add_component('ParameterTable')
            ds.add_component('LanguageTable')
            ds.add_table('contributions.csv', 'ID', 'Name', 'Description', 'Contributors')
            ds.add_columns(ds.primary_table, Column.fromvalue(
                {
                    'name': 'Contribution_ID',
                    'datatype': 'string',
                    'valueUrl': url_template(req, 'contribution', 'contribution').uri,
                }))
            ds.add_foreign_key(
                ds.primary_table, 'Contribution_ID', 'contributions.csv', 'ID')
            ds['LanguageTable'].aboutUrl = url_template(req, 'language', 'ID')
            ds['ParameterTable'].aboutUrl = url_template(req, 'parameter', 'ID')
            ds[ds.primary_table].aboutUrl = url_template(req, 'value', 'ID')

            cldf_cfg.custom_schema(req, ds)

            for src in cldf_cfg.query(Source):
                ds.sources.add(cldf_cfg.convert(Source, src, req))

            tabledata = defaultdict(list)
            for table, model in [
                ('ParameterTable', Parameter),
                ('LanguageTable', Language),
                ('ExampleTable', Sentence),
                ('contributions.csv', Contribution),
                (ds.primary_table, Value),
            ]:
                for item in cldf_cfg.query(model):
                    tabledata[table].append(cldf_cfg.convert(model, item, req))
            ds.write(**cldf_cfg.custom_tabledata(req, tabledata))
            ds.validate()

            fname = outfile or self.abspath(req)
            shutil.make_archive(
                fname.parent.joinpath(fname.stem).as_posix(), 'zip', tmpd.as_posix())

# encoding: utf-8

from pylons import config, cache
import sqlalchemy.exc

import ckan.logic as logic
import ckan.lib.maintain as maintain
import ckan.lib.search as search
import ckan.lib.base as base
import ckan.model as model
import ckan.lib.helpers as h

from ckan.common import _, g, c

from ckan.common import request, response, json
from ckan.lib.helpers import url_for

CACHE_PARAMETERS = ['__cache', '__no_cache__']


class HomeController(base.BaseController):
    repo = model.repo

    def __before__(self, action, **env):
        try:
            base.BaseController.__before__(self, action, **env)
            context = {'model': model, 'user': c.user,
                       'auth_user_obj': c.userobj}
            logic.check_access('site_read', context)
        except logic.NotAuthorized:
            base.abort(403, _('Not authorized to see this page'))
        except (sqlalchemy.exc.ProgrammingError,
                sqlalchemy.exc.OperationalError), e:
            # postgres and sqlite errors for missing tables
            msg = str(e)
            if ('relation' in msg and 'does not exist' in msg) or \
                    ('no such table' in msg):
                # table missing, major database problem
                base.abort(503, _('This site is currently off-line. Database '
                                  'is not initialised.'))
                # TODO: send an email to the admin person (#1285)
            else:
                raise

    def index(self):
        try:
            # package search
            context = {'model': model, 'session': model.Session,
                       'user': c.user, 'auth_user_obj': c.userobj}
            data_dict = {
                'q': '*:*',
                'facet.field': g.facets,
                'rows': 4,
                'start': 0,
                'sort': 'views_recent desc',
                'fq': 'capacity:"public"'
            }
            query = logic.get_action('package_search')(
                context, data_dict)
            c.search_facets = query['search_facets']
            c.package_count = query['count']
            c.datasets = query['results']

            c.facets = query['facets']
            maintain.deprecate_context_item(
                'facets',
                'Use `c.search_facets` instead.')

            c.search_facets = query['search_facets']

            c.facet_titles = {
                'organization': _('Organizations'),
                'groups': _('Groups'),
                'tags': _('Tags'),
                'res_format': _('Formats'),
                'license': _('Licenses'),
            }

        except search.SearchError:
            c.package_count = 0

        if c.userobj and not c.userobj.email:
            url = h.url_for(controller='user', action='edit')
            msg = _('Please <a href="%s">update your profile</a>'
                    ' and add your email address. ') % url + \
                _('%s uses your email address'
                    ' if you need to reset your password.') \
                % g.site_title
            h.flash_notice(msg, allow_html=True)

        return base.render('home/index.html', cache_force=True)

    def license(self):
        return base.render('home/license.html')

    def about(self):
        return base.render('home/about.html')

    def cache(self, id):
        '''Manual way to clear the caches'''
        if id == 'clear':
            wui_caches = ['stats']
            for cache_name in wui_caches:
                cache_ = cache.get_cache(cache_name, type='dbm')
                cache_.clear()
            return 'Cleared caches: %s' % ', '.join(wui_caches)

    def cors_options(self, url=None):
        # just return 200 OK and empty data
        return ''

    def test_flask_plus_pylons(self):
        # TODO: Move this to a test
        response.headers['Content-Type'] = 'application/json;charset=utf-8'

        url_pylons = url_for(controller='package', action='edit', id='test-id')

        url_pylons_external = url_for(controller='package', action='edit', id='test-id', qualified=True)

        url_flask_old_syntax = url_for(
            controller='api', action='action', ver='3',
            logic_function='package_show', id='test-id')

        url_flask_external_old_syntax = url_for(
            controller='api', action='action', ver='3',
            logic_function='package_show', id='test-id', qualified=True)

        url_flask_new_syntax = url_for(
            'api.action', ver=3,
            logic_function='package_search', q='-name:test-*',
            sort='name desc')

        url_flask_external_new_syntax = url_for(
            'api.action', ver=3,
            logic_function='package_search', q='-name:test-*',
            sort='name desc', _external=True)

        out = {
            'c_user': c.user,
            'lang_on_environ_CKAN_LANG': request.environ.get('CKAN_LANG'),
            'translated_string': _('Editor'),
            'url_from_pylons': url_pylons,
            'url_from_pylons_external': url_pylons_external,
            'url_from_flask_old_syntax': url_flask_old_syntax,
            'url_from_flask_new_syntax': url_flask_new_syntax,
            'url_from_flask_external_old_syntax': url_flask_external_old_syntax,
            'url_from_flask_external_new_syntax': url_flask_external_new_syntax,

        }

        return json.dumps(out)

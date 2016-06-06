import cgi
import logging

from flask import Blueprint, request, make_response, g, redirect
from werkzeug.exceptions import BadRequest

import ckan.model as model
from ckan.common import json, _, c
from ckan.lib.helpers import url_for

from ckan.lib.navl.dictization_functions import DataError
from ckan.logic import get_action, ValidationError, NotFound, NotAuthorized
from ckan.lib.search import SearchError, SearchIndexError, SearchQueryError
# import ckan.plugins as p


log = logging.getLogger(__name__)

CONTENT_TYPES = {
    'text': 'text/plain;charset=utf-8',
    'html': 'text/html;charset=utf-8',
    'json': 'application/json;charset=utf-8',
}


APIKEY_HEADER_NAME_KEY = 'apikey_header_name'
APIKEY_HEADER_NAME_DEFAULT = 'X-CKAN-API-Key'

API_DEFAULT_VERSION = 3
API_MAX_VERSION = 3


# Blueprint definition

api = Blueprint('api', __name__, url_prefix='/api')


# Private methods


def _identify_user():
    '''Try to identify the user
    If the user is identified then:
      c.user = user name (unicode)
      c.userobj = user object
      c.author = user name
    otherwise:
      c.user = None
      c.userobj = None
      c.author = user's IP address (unicode)'''
    # see if it was proxied first
    c.remote_addr = request.environ.get('HTTP_X_FORWARDED_FOR', '')
    if not c.remote_addr:
        c.remote_addr = request.environ.get('REMOTE_ADDR',
                                            'Unknown IP Address')

    # TODO:
    # Authentication plugins get a chance to run here break as soon as a
    # user is identified.
    # authenticators = p.PluginImplementations(p.IAuthenticator)
    # if authenticators:
    #    for item in authenticators:
    #        item.identify()
    #        if c.user:
    #            break

    # We haven't identified the user so try the default methods
    if not getattr(c, 'user', None):
        _identify_user_default()

    # If we have a user but not the userobj let's get the userobj.  This
    # means that IAuthenticator extensions do not need to access the user
    # model directly.
    if c.user and not getattr(c, 'userobj', None):
        c.userobj = model.User.by_name(c.user)

    # general settings
    if c.user:
        c.author = c.user
    else:
        c.author = c.remote_addr
    c.author = unicode(c.author)


def _identify_user_default():
    '''
    Identifies the user using two methods:
    a) If they logged into the web interface then repoze.who will
       set REMOTE_USER.
    b) For API calls they may set a header with an API key.
    '''

    # environ['REMOTE_USER'] is set by repoze.who if it authenticates a
    # user's cookie. But repoze.who doesn't check the user (still) exists
    # in our database - we need to do that here. (Another way would be
    # with an userid_checker, but that would mean another db access.
    # See: http://docs.repoze.org/who/1.0/narr.html#module-repoze.who\
    # .plugins.sql )
    g.user = request.environ.get('REMOTE_USER', '')
    if g.user:
        g.user = g.user.decode('utf8')
        g.userobj = model.User.by_name(g.user)
        if g.userobj is None or not g.userobj.is_active():

            # This occurs when a user that was still logged in is deleted, or
            # when you are logged in, clean db and then restart (or when you
            # change your username). There is no user object, so even though
            # repoze thinks you are logged in and your cookie has
            # ckan_display_name, we need to force user to logout and login
            # again to get the User object.

            ev = request.environ
            if 'repoze.who.plugins' in ev:
                pth = getattr(ev['repoze.who.plugins']['friendlyform'],
                              'logout_handler_path')
                redirect(pth)
    else:
        c.userobj = _get_user_for_apikey()
        if c.userobj is not None:
            c.user = c.userobj.name


def _get_user_for_apikey():
    # TODO: use config
    # apikey_header_name = config.get(APIKEY_HEADER_NAME_KEY,
    #                                APIKEY_HEADER_NAME_DEFAULT)
    apikey_header_name = APIKEY_HEADER_NAME_DEFAULT
    apikey = request.headers.get(apikey_header_name, '')
    if not apikey:
        apikey = request.environ.get(apikey_header_name, '')
    if not apikey:
        # For misunderstanding old documentation (now fixed).
        apikey = request.environ.get('HTTP_AUTHORIZATION', '')
    if not apikey:
        apikey = request.environ.get('Authorization', '')
        # Forget HTTP Auth credentials (they have spaces).
        if ' ' in apikey:
            apikey = ''
    if not apikey:
        return None
    log.debug("Received API Key: %s" % apikey)
    apikey = unicode(apikey)
    query = model.Session.query(model.User)
    user = query.filter_by(apikey=apikey).first()
    return user


def _finish(status_int, response_data=None,
            content_type='text', headers=None):
    '''When a controller method has completed, call this method
    to prepare the response.
    @return response message - return this value from the controller
                               method
             e.g. return _finish(404, 'Package not found')
    '''
    assert(isinstance(status_int, int))
    response_msg = ''
    if headers is None:
        headers = {}
    if response_data is not None:
        headers['Content-Type'] = CONTENT_TYPES[content_type]
        if content_type == 'json':
            response_msg = json.dumps(
                response_data,
                for_json=True)  # handle objects with for_json methods
        else:
            response_msg = response_data
        # Support "JSONP" callback.
        if (status_int == 200 and 'callback' in request.args and
                request.method == 'GET'):
            # escape callback to remove '<', '&', '>' chars
            callback = cgi.escape(request.args['callback'])
            response_msg = _wrap_jsonp(callback, response_msg)
    return make_response((response_msg, status_int, headers))


def _finish_ok(response_data=None,
               content_type='json',
               resource_location=None):
    '''If a controller method has completed successfully then
    calling this method will prepare the response.
    @param resource_location - specify this if a new
       resource has just been created.
    @return response message - return this value from the controller
                               method
                               e.g. return _finish_ok(pkg_dict)
    '''
    status_int = 200
    headers = None
    if resource_location:
        status_int = 201
        try:
            resource_location = str(resource_location)
        except Exception, inst:
            msg = \
                "Couldn't convert '%s' header value '%s' to string: %s" % \
                ('Location', resource_location, inst)
            raise Exception(msg)
        headers = {'Location': resource_location}

    return _finish(status_int, response_data, content_type, headers)


def _finish_not_authz(extra_msg=None):
    response_data = _('Access denied')
    if extra_msg:
        response_data = '%s - %s' % (response_data, extra_msg)
    return _finish(403, response_data, 'json')


def _finish_not_found(extra_msg=None):
    response_data = _('Not found')
    if extra_msg:
        response_data = '%s - %s' % (response_data, extra_msg)
    return _finish(404, response_data, 'json')


def _finish_bad_request(extra_msg=None):
    response_data = _('Bad request')
    if extra_msg:
        response_data = '%s - %s' % (response_data, extra_msg)
    return _finish(400, response_data, 'json')


def _wrap_jsonp(callback, response_msg):
    return '%s(%s);' % (callback, response_msg)


def _get_request_data(try_url_params=False):
    '''Returns a dictionary, extracted from a request.

    If there is no data, None or "" is returned.
    ValueError will be raised if the data is not a JSON-formatted dict.

    The data is retrieved as a JSON-encoded dictionary from the request
    body.  Or, if the `try_url_params` argument is True and the request is
    a GET request, then an attempt is made to read the data from the url
    parameters of the request.

    try_url_params
        If try_url_params is False, then the data_dict is read from the
        request body.

        If try_url_params is True and the request is a GET request then the
        data is read from the url parameters.  The resulting dict will only
        be 1 level deep, with the url-param fields being the keys.  If a
        single key has more than one value specified, then the value will
        be a list of strings, otherwise just a string.

    '''
    def make_unicode(entity):
        '''Cast bare strings and strings in lists or dicts to Unicode. '''
        if isinstance(entity, str):
            return unicode(entity)
        elif isinstance(entity, list):
            new_items = []
            for item in entity:
                new_items.append(make_unicode(item))
            return new_items
        elif isinstance(entity, dict):
            new_dict = {}
            for key, val in entity.items():
                new_dict[key] = make_unicode(val)
            return new_dict
        else:
            return entity

    def mixed(multi_dict):
        '''Return a dict with values being lists if they have more than one
           item or a string otherwise
        '''
        out = {}
        for key, value in multi_dict.to_dict(flat=False).iteritems():
            out[key] = value[0] if len(value) == 1 else value
        return out

    if not try_url_params and request.method == 'GET':
        raise ValueError('Invalid request. Please use POST method '
                         'for your request')

    request_data = {}
    if request.method == 'POST' and request.form:
        if (len(request.form.values()) == 1 and
                request.form.values()[0] in [u'1', u'']):
            try:
                request_data = json.loads(request.form.keys()[0])
            except ValueError, e:
                raise ValueError(
                    'Error decoding JSON data. '
                    'Error: %r '
                    'JSON data extracted from the request: %r' %
                    (e, request_data))
        else:
            request_data = mixed(request.form)
    elif request.args and try_url_params:
        request_data = mixed(request.args)
    elif (request.data and request.data != '' and
          request.content_type != 'multipart/form-data'):
        try:
            request_data = request.get_json()
        except BadRequest, e:
            raise ValueError('Error decoding JSON data. '
                             'Error: %r '
                             'JSON data extracted from the request: %r' %
                             (e, request_data))
    if not isinstance(request_data, dict):
        raise ValueError('Request data JSON decoded to %r but '
                         'it needs to be a dictionary.' % request_data)
    if request_data:
        # ensure unicode values
        for key, val in request_data.items():
            # if val is str then assume it is ascii, since json converts
            # utf8 encoded JSON to unicode
            request_data[key] = make_unicode(val)
    log.debug('Request data extracted: %r', request_data)
    return request_data


# View functions

def action(logic_function, ver=API_DEFAULT_VERSION):

    try:
        function = get_action(logic_function)
    except KeyError:
        msg = 'Action name not known: {0}'.format(logic_function)
        log.info(msg)
        return _finish_bad_request(msg)

    # TODO: Abstract to base class
    _identify_user()

    context = {'model': model, 'session': model.Session, 'user': c.user,
               'api_version': ver, 'auth_user_obj': c.userobj}
    model.Session()._context = context

    # TODO: backwards-compatible named routes?
    return_dict = {'help': url_for('api.action',
                                   logic_function='help_show',
                                   ver=ver,
                                   name=logic_function,
                                   _external=True,
                                   )
                   }
    try:
        side_effect_free = getattr(function, 'side_effect_free', False)

        request_data = _get_request_data(
            try_url_params=side_effect_free)
    except ValueError, inst:
        log.info('Bad Action API request data: %s', inst)
        return _finish_bad_request(
            _('JSON Error: %s') % inst)
    if not isinstance(request_data, dict):
        # this occurs if request_data is blank
        log.info('Bad Action API request data - not dict: %r',
                 request_data)
        return _finish_bad_request(
            _('Bad request data: %s') %
            'Request data JSON decoded to %r but '
            'it needs to be a dictionary.' % request_data)

    # if callback is specified we do not want to send that to the search
    if 'callback' in request_data:
        del request_data['callback']
        c.user = None
        c.userobj = None
        context['user'] = None
        context['auth_user_obj'] = None
    try:
        result = function(context, request_data)
        return_dict['success'] = True
        return_dict['result'] = result
    except DataError, e:
        log.info('Format incorrect (Action API): %s - %s',
                 e.error, request_data)
        return_dict['error'] = {'__type': 'Integrity Error',
                                'message': e.error,
                                'data': request_data}
        return_dict['success'] = False
        return _finish(400, return_dict, content_type='json')
    except NotAuthorized, e:
        return_dict['error'] = {'__type': 'Authorization Error',
                                'message': _('Access denied')}
        return_dict['success'] = False

        if unicode(e):
            return_dict['error']['message'] += u': %s' % e

        return _finish(403, return_dict, content_type='json')
    except NotFound, e:
        return_dict['error'] = {'__type': 'Not Found Error',
                                'message': _('Not found')}
        if unicode(e):
            return_dict['error']['message'] += u': %s' % e
        return_dict['success'] = False
        return _finish(404, return_dict, content_type='json')
    except ValidationError, e:
        error_dict = e.error_dict
        error_dict['__type'] = 'Validation Error'
        return_dict['error'] = error_dict
        return_dict['success'] = False
        # CS nasty_string ignore
        log.info('Validation error (Action API): %r', str(e.error_dict))
        return _finish(409, return_dict, content_type='json')
    except SearchQueryError, e:
        return_dict['error'] = {'__type': 'Search Query Error',
                                'message': 'Search Query is invalid: %r' %
                                e.args}
        return_dict['success'] = False
        return _finish(400, return_dict, content_type='json')
    except SearchError, e:
        return_dict['error'] = {'__type': 'Search Error',
                                'message': 'Search error: %r' % e.args}
        return_dict['success'] = False
        return _finish(409, return_dict, content_type='json')
    except SearchIndexError, e:
        return_dict['error'] = {
            '__type': 'Search Index Error',
            'message': 'Unable to add package to search index: %s' %
                       str(e)}
        return_dict['success'] = False
        return _finish(500, return_dict, content_type='json')
    return _finish_ok(return_dict)


def get_api(ver=1):
    response_data = {
        'version': ver
    }
    return _finish_ok(response_data)


def test_flask_plus_pylons():

    _identify_user()

    # TODO: Move this to a test
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

    return _finish_ok(out)


# Routing


api.add_url_rule('/', view_func=get_api, strict_slashes=False)

api.add_url_rule('/test_flask_plus_pylons', view_func=test_flask_plus_pylons, strict_slashes=False)
api.add_url_rule('/action/<logic_function>', methods=['GET', 'POST'],
                 view_func=action)
api.add_url_rule('/<int(min=3, max={0}):ver>/action/<logic_function>'.format(
                 API_MAX_VERSION),
                 methods=['GET', 'POST'],
                 view_func=action)

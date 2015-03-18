import hashlib
import hmac

from fxa.oauth import Client as OAuthClient
from fxa import errors as fxa_errors
from pyramid import authentication as base_auth
from pyramid import httpexceptions
from pyramid.interfaces import IAuthenticationPolicy, IAuthorizationPolicy
from pyramid.security import Authenticated
from zope.interface import implementer

from cliquet import cache
from cliquet import logger


def check_credentials(username, password, request):
    """Basic auth implementation.

    Allow any user with any credentials (e.g. there is no need to create an
    account).

    """
    settings = request.registry.settings
    is_enabled = settings['cliquet.basic_auth_enabled']

    if not is_enabled or not username:
        return

    hmac_secret = settings['cliquet.userid_hmac_secret'].encode('utf-8')
    credentials = '%s:%s' % (username, password)
    userid = hmac.new(hmac_secret,
                      credentials.encode('utf-8'),
                      hashlib.sha256).hexdigest()

    # Log authentication context.
    logger.bind(auth_type='Basic')

    return ["basicauth_%s" % userid]


class BasicAuthAuthenticationPolicy(base_auth.BasicAuthAuthenticationPolicy):
    def __init__(self, *args, **kwargs):
        super(BasicAuthAuthenticationPolicy, self).__init__(check_credentials,
                                                            *args,
                                                            **kwargs)


@implementer(IAuthenticationPolicy)
class Oauth2AuthenticationPolicy(base_auth.CallbackAuthenticationPolicy):
    def __init__(self, config, realm='Realm'):
        self.realm = realm

        settings = config.get_settings()
        oauth_cache_ttl = int(settings['fxa-oauth.cache_ttl_seconds'])
        oauth_cache = cache.SessionCache(config.registry.cache,
                                         ttl=oauth_cache_ttl)
        self.cache = oauth_cache

    def unauthenticated_userid(self, request):
        user_id = self._get_credentials(request)
        return user_id

    def forget(self, request):
        """A no-op. Credentials are sent on every request.
        Return WWW-Authenticate Realm header for Bearer token.
        """
        return [('WWW-Authenticate', 'Bearer realm="%s"' % self.realm)]

    def _get_credentials(self, request):
        authorization = request.headers.get('Authorization', '')
        settings = request.registry.settings

        try:
            authmeth, auth = authorization.split(' ', 1)
            assert authmeth.lower() == 'bearer'
        except (AssertionError, ValueError):
            return None

        # Use PyFxa defaults if not specified
        server_url = settings['fxa-oauth.oauth_uri']
        scope = settings['fxa-oauth.scope']

        auth_client = OAuthClient(server_url=server_url, cache=self.cache)
        try:
            profile = auth_client.verify_token(token=auth, scope=scope)
            user_id = profile['user'].encode('utf-8')
        except fxa_errors.OutOfProtocolError:
            raise httpexceptions.HTTPServiceUnavailable()
        except (fxa_errors.InProtocolError, fxa_errors.TrustError):
            return None

        # Log authentication context.
        logger.bind(auth_type='FxA')

        return 'fxa_%s' % user_id


@implementer(IAuthorizationPolicy)
class AuthorizationPolicy(object):
    def permits(self, context, principals, permission):
        """Currently we don't check scopes nor permissions.
        Authenticated users only are allowed.
        """
        PERMISSIONS = {
            'readonly': Authenticated,
            'readwrite': Authenticated,
        }
        role = PERMISSIONS.get(permission)
        return role and role in principals

    def principals_allowed_by_permission(self, context, permission):
        raise NotImplementedError()  # PRAGMA NOCOVER
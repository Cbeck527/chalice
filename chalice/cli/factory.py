import sys
import os
import json
import importlib
import logging

from botocore.session import Session
from typing import Any, Optional, Dict  # noqa

from chalice import __version__ as chalice_version
from chalice.awsclient import TypedAWSClient
from chalice.app import Chalice  # noqa
from chalice.config import Config
from chalice.deploy import deployer
from chalice.package import create_app_packager
from chalice.package import AppPackager  # noqa
from chalice.constants import DEFAULT_STAGE_NAME
from chalice.logs import LogRetriever


def create_botocore_session(profile=None, debug=False):
    # type: (str, bool) -> Session
    s = Session(profile=profile)
    _add_chalice_user_agent(s)
    if debug:
        s.set_debug_logger('')
        _inject_large_request_body_filter()
    return s


def _add_chalice_user_agent(session):
    # type: (Session) -> None
    suffix = '%s/%s' % (session.user_agent_name, session.user_agent_version)
    session.user_agent_name = 'aws-chalice'
    session.user_agent_version = chalice_version
    session.user_agent_extra = suffix


def _inject_large_request_body_filter():
    # type: () -> None
    log = logging.getLogger('botocore.endpoint')
    log.addFilter(LargeRequestBodyFilter())


class UnknownConfigFileVersion(Exception):
    def __init__(self, version):
        # type: (str) -> None
        super(UnknownConfigFileVersion, self).__init__(
            "Unknown version '%s' in config.json" % version)


class LargeRequestBodyFilter(logging.Filter):
    def filter(self, record):
        # type: (Any) -> bool
        # Note: the proper type should be "logging.LogRecord", but
        # the typechecker complains about 'Invalid index type "int" for "dict"'
        # so we're using Any for now.
        if record.msg.startswith('Making request'):
            if record.args[0].name in ['UpdateFunctionCode', 'CreateFunction']:
                # When using the ZipFile argument (which is used in chalice),
                # the entire deployment package zip is sent as a base64 encoded
                # string.  We don't want this to clutter the debug logs
                # so we don't log the request body for lambda operations
                # that have the ZipFile arg.
                record.args = (record.args[:-1] +
                               ('(... omitted from logs due to size ...)',))
        return True


class CLIFactory(object):
    def __init__(self, project_dir, debug=False, profile=None):
        # type: (str, bool, Optional[str]) -> None
        self.project_dir = project_dir
        self.debug = debug
        self.profile = profile

    def create_botocore_session(self):
        # type: () -> Session
        return create_botocore_session(profile=self.profile,
                                       debug=self.debug)

    def create_default_deployer(self, session, prompter):
        # type: (Session, deployer.NoPrompt) -> deployer.Deployer
        return deployer.create_default_deployer(
            session=session, prompter=prompter)

    def create_config_obj(self, chalice_stage_name=DEFAULT_STAGE_NAME,
                          autogen_policy=None, api_gateway_stage=None):
        # type: (str, Optional[bool], Optional[str]) -> Config
        user_provided_params = {}  # type: Dict[str, Any]
        default_params = {'project_dir': self.project_dir,
                          'autogen_policy': True}
        try:
            config_from_disk = self.load_project_config()
        except (OSError, IOError):
            raise RuntimeError("Unable to load the project config file. "
                               "Are you sure this is a chalice project?")
        self._validate_config_from_disk(config_from_disk)
        app_obj = self.load_chalice_app()
        user_provided_params['chalice_app'] = app_obj
        if autogen_policy is not None:
            user_provided_params['autogen_policy'] = autogen_policy
        if self.profile is not None:
            user_provided_params['profile'] = self.profile
        if api_gateway_stage is not None:
            user_provided_params['api_gateway_stage'] = api_gateway_stage
        config = Config(chalice_stage=chalice_stage_name,
                        user_provided_params=user_provided_params,
                        config_from_disk=config_from_disk,
                        default_params=default_params)
        return config

    def _validate_config_from_disk(self, config):
        # type: (Dict[str, Any]) -> None
        string_version = config.get('version', '1.0')
        try:
            version = float(string_version)
            if version > 2.0:
                raise UnknownConfigFileVersion(string_version)
        except ValueError:
            raise UnknownConfigFileVersion(string_version)

    def create_app_packager(self, config):
        # type: (Config) -> AppPackager
        return create_app_packager(config)

    def create_log_retriever(self, session, lambda_arn):
        # type: (Session, str) -> LogRetriever
        client = TypedAWSClient(session)
        retriever = LogRetriever.create_from_arn(client, lambda_arn)
        return retriever

    def load_chalice_app(self):
        # type: () -> Chalice
        if self.project_dir not in sys.path:
            sys.path.insert(0, self.project_dir)
        # The vendor directory has its contents copied up to the top level of
        # the deployment package. This means that imports will work in the
        # lambda function as if the vendor directory is on the python path.
        # For loading the config locally we must add the vendor directory to
        # the path so it will be treated the same as if it were running on
        # lambda.
        vendor_dir = os.path.join(self.project_dir, 'vendor')
        if os.path.isdir(vendor_dir) and vendor_dir not in sys.path:
            # This is a tradeoff we have to make for local use.
            # The common use case of vendor/ is to include
            # extension modules built for AWS Lambda.  If you're
            # running on a non-linux dev machine, then attempting
            # to import these files will raise exceptions.  As
            # a workaround, the vendor is added to the end of
            # sys.path so it's after `./lib/site-packages`.
            # This gives you a change to install the correct
            # version locally and still keep the lambda
            # specific one in vendor/
            sys.path.append(vendor_dir)
        try:
            app = importlib.import_module('app')
            chalice_app = getattr(app, 'app')
        except SyntaxError as e:
            message = (
                'Unable to import your app.py file:\n\n'
                'File "%s", line %s\n'
                '  %s\n'
                'SyntaxError: %s'
            ) % (getattr(e, 'filename'), e.lineno, e.text, e.msg)
            raise RuntimeError(message)
        return chalice_app

    def load_project_config(self):
        # type: () -> Dict[str, Any]
        """Load the chalice config file from the project directory.

        :raise: OSError/IOError if unable to load the config file.

        """
        config_file = os.path.join(self.project_dir, '.chalice', 'config.json')
        with open(config_file) as f:
            return json.loads(f.read())

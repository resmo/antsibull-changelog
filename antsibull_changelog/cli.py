# coding: utf-8
# Author: Felix Fontein <felix@fontein.de>
# License: GPLv3+
# Copyright: Ansible Project, 2020

"""
Entrypoint to the antsibull-changelog script.
"""

import argparse
import datetime
import os
import sys
import traceback

from typing import Any, List, Optional, Tuple, Union

try:
    import argcomplete
    HAS_ARGCOMPLETE = True
except ImportError:
    HAS_ARGCOMPLETE = False

from .ansible import get_ansible_release
from .changelog_generator import generate_changelog
from .changes import load_changes, add_release, refresh_changelog
from .config import ChangelogConfig, CollectionDetails, PathsConfig
from .errors import ChangelogError
from .fragment import load_fragments, ChangelogFragment, ChangelogFragmentLinter
from .plugins import load_plugins, PluginDescription
from .logger import LOGGER, setup_logger


def set_paths(force: Optional[str] = None,
              is_collection: Optional[bool] = None,
              ansible_doc_bin: Optional[str] = None) -> PathsConfig:
    """
    Create ``PathsConfig``.

    :arg force: If ``True``, create a collection path config for the given path.
                Otherwise, detect configuration.
    :arg is_collection: Override detection of whether the tool is run in a collection
                        or in ansible-base.
    :arg ansible_doc_bin: Override path to ansible-doc.
    """
    if force:
        if is_collection is False:
            return PathsConfig.force_ansible(force, ansible_doc_bin=ansible_doc_bin)
        return PathsConfig.force_collection(force, ansible_doc_bin=ansible_doc_bin)

    try:
        return PathsConfig.detect(is_collection=is_collection, ansible_doc_bin=ansible_doc_bin)
    except ChangelogError:
        if is_collection is True:
            raise ChangelogError(
                "Only the 'init' command can be used outside a collection repository, "
                "or inside one without changelogs/config.yaml.")
        if is_collection is False:
            raise ChangelogError(
                "Only the 'init' command can be used outside an Ansible checkout, "
                "or inside one without changelogs/config.yaml.")
        raise ChangelogError(
            "Only the 'init' command can be used outside an Ansible checkout and outside a "
            "collection repository, or inside one without changelogs/config.yaml.\n"
            "If you are in a collection without galaxy.yml, specify `--is-collection no` "
            "on the command line.")


def parse_boolean_arg(value: Any) -> bool:
    """
    Parse a string as a boolean
    """
    if isinstance(value, bool):
        return value
    value = str(value)
    if value.lower() in ('yes', 'true'):
        return True
    if value.lower() in ('no', 'false'):
        return False
    raise argparse.ArgumentTypeError('Cannot interpret as boolean')


def create_argparser(program_name: str) -> argparse.ArgumentParser:
    """
    Create CLI argument parser.
    """
    parser = argparse.ArgumentParser(
        prog=program_name,
        description='Changelog generator and linter.')

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-v', '--verbose',
                        action='count',
                        default=0,
                        help='increase verbosity of output')

    is_collection = argparse.ArgumentParser(add_help=False)
    is_collection.add_argument('--is-collection',
                               type=parse_boolean_arg,
                               help='override whether this is a collection or not '
                                    '(needed when galaxy.yml does not exist)')

    collection_details = argparse.ArgumentParser(add_help=False)
    collection_details.add_argument('--collection-namespace',
                                    help='set collection namespace '
                                         '(needed when galaxy.yml does not exist)')
    collection_details.add_argument('--collection-name',
                                    help='set collection name '
                                         '(needed when galaxy.yml does not exist)')
    collection_details.add_argument('--collection-flatmap',
                                    type=parse_boolean_arg,
                                    help='override collection flatmapping flag '
                                         '(needed when galaxy.yml does not exist)')

    subparsers = parser.add_subparsers(metavar='COMMAND')

    init_parser = subparsers.add_parser('init',
                                        parents=[common],
                                        help='set up changelog infrastructure for collection')
    init_parser.set_defaults(func=command_init)
    init_parser.add_argument('root',
                             metavar='COLLECTION_ROOT',
                             help='path to collection root')

    lint_parser = subparsers.add_parser('lint',
                                        parents=[common],
                                        help='check changelog fragments for syntax errors')
    lint_parser.set_defaults(func=command_lint)
    lint_parser.add_argument('fragments',
                             metavar='FRAGMENT',
                             nargs='*',
                             help='path to fragment to test')

    release_parser = subparsers.add_parser('release',
                                           parents=[common, is_collection, collection_details],
                                           help='add a new release to the change metadata')
    release_parser.set_defaults(func=command_release)
    release_parser.add_argument('--version',
                                help='override release version')
    release_parser.add_argument('--codename',
                                help='override/set release codename')
    release_parser.add_argument('--date',
                                default=str(datetime.date.today()),
                                help='override release date')
    release_parser.add_argument('--reload-plugins',
                                action='store_true',
                                help='force reload of plugin cache')
    release_parser.add_argument('--ansible-doc-bin',
                                help='path to ansible-doc (overrides autodetect)')
    release_parser.add_argument('--use-ansible-doc',
                                action='store_true',
                                help='always use ansible-doc to find plugins')
    release_parser.add_argument('--refresh',
                                action='store_true',
                                help='update existing entries from fragment files (if '
                                     'keep_fragments is true), and update plugin descriptions '
                                     '(should be combined with --reload-plugins)')

    generate_parser = subparsers.add_parser('generate',
                                            parents=[common, is_collection, collection_details],
                                            help='generate the changelog')
    generate_parser.set_defaults(func=command_generate)
    generate_parser.add_argument('--reload-plugins',
                                 action='store_true',
                                 help='force reload of plugin cache')
    generate_parser.add_argument('--ansible-doc-bin',
                                 help='path to ansible-doc (overrides autodetect)')
    generate_parser.add_argument('--use-ansible-doc',
                                 action='store_true',
                                 help='always use ansible-doc to find plugins')
    generate_parser.add_argument('--refresh',
                                 action='store_true',
                                 help='update existing entries from fragment files (if '
                                      'keep_fragments is true), and update plugin descriptions '
                                      '(should be combined with --reload-plugins)')

    if HAS_ARGCOMPLETE:
        argcomplete.autocomplete(parser)

    return parser


def load_collection_details(collection_details: CollectionDetails, args: Any):
    """
    Override collection details data with CLI args.
    """
    if args.collection_namespace is not None:
        collection_details.namespace = args.collection_namespace
    if args.collection_name is not None:
        collection_details.name = args.collection_name
    if args.collection_flatmap is not None:
        collection_details.flatmap = args.collection_flatmap


def run(args: List[str]) -> int:
    """
    Main program entry point.
    """
    verbosity = 0
    try:
        program_name = os.path.basename(args[0])
        parser = create_argparser(program_name)

        arguments = parser.parse_args(args[1:])

        if getattr(arguments, 'func', None) is None:
            parser.print_help()
            return 2

        verbosity = arguments.verbose
        setup_logger(verbosity)

        return arguments.func(arguments)
    except ChangelogError as e:
        LOGGER.error(str(e))
        if verbosity > 2:
            traceback.print_exc()
        return 5
    except SystemExit as e:
        return e.code
    except Exception:  # pylint: disable=broad-except
        if verbosity > 0:
            traceback.print_exc()
        else:
            print('ERROR: Uncaught exception. Run with -v to see traceback.')
        return 1


def command_init(args: Any) -> int:
    """
    Initialize a changelog config.

    :arg args: Parsed arguments
    """
    root: str = args.root

    paths = set_paths(force=root)

    if paths.galaxy_path is None:
        LOGGER.error('The file galaxy.yml does not exists in the collection root!')
        return 5
    LOGGER.debug('Checking for existance of "{}"', paths.config_path)
    if os.path.exists(paths.config_path):
        LOGGER.error('A configuration file already exists at "{}"!', paths.config_path)
        return 5

    collection_details = CollectionDetails(paths)

    config = ChangelogConfig.default(
        paths,
        collection_details,
        title='{0}.{1}'.format(
            collection_details.get_namespace().title(), collection_details.get_name().title()),
    )

    fragments_dir = os.path.join(paths.changelog_dir, config.notes_dir)
    try:
        os.makedirs(fragments_dir, exist_ok=True)
        print('Created fragments directory "{0}"'.format(fragments_dir))
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.error('Cannot create fragments directory "{}"', fragments_dir)
        LOGGER.info('Exception: {}', str(exc))
        return 5

    try:
        config.store()
        print('Created config file "{0}"'.format(paths.config_path))
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.error('Cannot create config file "{}"', paths.config_path)
        LOGGER.info('Exception: {}', str(exc))
        return 5

    return 0


def _determine_flatmap(collection_details: CollectionDetails,
                       config: ChangelogConfig) -> bool:
    """
    Determine whether flatmapping is used or not for the collection.
    """
    flatmap = not config.is_collection
    flatmap_ = config.flatmap
    if flatmap_ is not None:
        flatmap = flatmap_
    if config.is_collection:
        flatmap_ = collection_details.get_flatmap()
        if flatmap_ is not None:
            flatmap = flatmap_
    return flatmap


def command_release(args: Any) -> int:
    """
    Add a new release to a changelog.

    :arg args: Parsed arguments
    """
    ansible_doc_bin: Optional[str] = args.ansible_doc_bin
    paths = set_paths(is_collection=args.is_collection, ansible_doc_bin=ansible_doc_bin)

    version: Union[str, None] = args.version
    codename: Union[str, None] = args.codename
    date = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
    reload_plugins: bool = args.reload_plugins
    use_ansible_doc: bool = args.use_ansible_doc

    collection_details = CollectionDetails(paths)
    config = ChangelogConfig.load(paths, collection_details)

    load_collection_details(collection_details, args)

    flatmap = _determine_flatmap(collection_details, config)

    if not version or not codename:
        if not config.is_collection:
            # Both version and codename are required for Ansible (Base)
            try:
                version, codename = get_ansible_release()
            except ValueError:
                LOGGER.error('Cannot import ansible.release to determine version and codename')
                return 5

        elif not version:
            # Codename is not required for collections, only version is
            version = collection_details.get_version()

    changes = load_changes(config)
    plugins = load_plugins(paths=paths, collection_details=collection_details,
                           version=version, force_reload=reload_plugins,
                           use_ansible_doc=use_ansible_doc)
    fragments = load_fragments(paths, config)
    if args.refresh or config.always_refresh:
        refresh_changelog(config, changes, plugins, fragments)
    add_release(config, changes, plugins, fragments, version, codename, date)
    generate_changelog(paths, config, changes, plugins, fragments, flatmap=flatmap)

    return 0


def command_generate(args: Any) -> int:
    """
    (Re-)generate the reStructuredText version of the changelog.

    :arg args: Parsed arguments
    """
    ansible_doc_bin: Optional[str] = args.ansible_doc_bin
    paths = set_paths(is_collection=args.is_collection, ansible_doc_bin=ansible_doc_bin)

    reload_plugins: bool = args.reload_plugins
    use_ansible_doc: bool = args.use_ansible_doc

    collection_details = CollectionDetails(paths)
    config = ChangelogConfig.load(paths, collection_details)

    load_collection_details(collection_details, args)

    flatmap = _determine_flatmap(collection_details, config)

    changes = load_changes(config)
    if not changes.has_release:
        print('Cannot create changelog when not at least one release has been added.')
        return 5
    plugins: Optional[List[PluginDescription]] = None
    fragments: Optional[List[ChangelogFragment]]
    if args.refresh or config.always_refresh:
        plugins = load_plugins(paths=paths, collection_details=collection_details,
                               version=changes.latest_version, force_reload=reload_plugins,
                               use_ansible_doc=use_ansible_doc)
        fragments = load_fragments(paths, config)
        refresh_changelog(config, changes, plugins, fragments)
    else:
        fragments = None
        if reload_plugins:
            plugins = load_plugins(paths=paths, collection_details=collection_details,
                                   version=changes.latest_version, force_reload=reload_plugins)
    generate_changelog(paths, config, changes, plugins, fragments, flatmap=flatmap)

    return 0


def command_lint(args: Any) -> int:
    """
    Lint changelog fragments.

    :arg args: Parsed arguments
    """
    # Passing is_collection=True ensures that we just look for changelogs/config.yaml,
    # and don't expect galaxy.yml or lib/ansible to be present.
    paths = set_paths(is_collection=True)

    fragment_paths: List[str] = args.fragments

    collection_details = CollectionDetails(paths)
    config = ChangelogConfig.load(paths, collection_details)

    exceptions: List[Tuple[str, Exception]] = []
    fragments = load_fragments(paths, config, fragment_paths, exceptions)
    return lint_fragments(config, fragments, exceptions)


def lint_fragments(config: ChangelogConfig, fragments: List[ChangelogFragment],
                   exceptions: List[Tuple[str, Exception]]) -> int:
    """
    Lint a given set of changelog fragment objects.

    :arg config: The configuration
    :arg fragments: The loaded fragments
    :arg exceptions: Exceptions from loading the fragments
    """
    linter = ChangelogFragmentLinter(config)

    errors = [(ex[0], 0, 0, 'yaml parsing error') for ex in exceptions]

    for fragment in fragments:
        errors += linter.lint(fragment)

    messages = sorted(set(
        '%s:%d:%d: %s' % (os.path.relpath(error[0]), error[1], error[2], error[3])
        for error in errors))

    for message in messages:
        print(message)

    return 3 if messages else 0


def main() -> int:
    """
    Entrypoint called from the script.

    console_scripts call functions which take no parameters.  However, it's hard to test a function
    which takes no parameters so this function lightly wraps :func:`run`, which actually does the
    heavy lifting.

    :returns: A program return code.

    Return codes:
        :0: Success
        :1: Unhandled error.  See the Traceback for more information.
        :2: There was a problem with the command line arguments
        :3: Found invalid changelog fragments
        :4: Needs to be run on a newer version of Python
        :5: Problem occured which prevented the execution of the command
    """
    if sys.version_info < (3, 6):
        print('Needs Python 3.6 or later')
        return 4

    return run(sys.argv)

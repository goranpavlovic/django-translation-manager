# -*- coding: utf-8 -*-
from copy import deepcopy

import os, sys
from optparse import make_option

import django
from django.core.management.commands.makemessages import Command as OriginCommand
from django.core.management.base import NoArgsCommand
from django.conf import settings

from project.translation_manager.manager import Manager
from project.translation_manager.settings import get_settings


################################################################################

class Command(OriginCommand):
    option_list = NoArgsCommand.option_list + (
        make_option('--locale', '-l', default=None, dest='locale', action='append',
            help='Creates or updates the message files for the given locale(s) (e.g. pt_BR). '
                 'Can be used multiple times.'),
        make_option('--domain', '-d', default=get_settings('TRANSLATIONS_DOMAINS') or ['django', 'djangojs'], dest='domain',
            help='The domain of the message files (default: "django").'),
        make_option('--all', '-a', action='store_true', dest='all',
            default=True, help='Updates the message files for all existing locales.'),
        make_option('--extension', '-e', dest='extensions',
            help='The file extension(s) to examine (default: "html,txt", or "js" if the domain is "djangojs"). Separate multiple extensions with commas, or use -e multiple times.',
            action='append'),
        make_option('--symlinks', '-s', action='store_true', dest='symlinks',
            default=False, help='Follows symlinks to directories when examining source code and templates for translation strings.'),
        make_option('--ignore', '-i', action='append', dest='ignore_patterns',
            default=get_settings('TRANSLATIONS_IGNORED_PATHS') or [], metavar='PATTERN', help='Ignore files or directories matching this glob-style pattern. Use multiple times to ignore more.'),
        make_option('--no-default-ignore', action='store_false', dest='use_default_ignore_patterns',
            default=True, help="Don't ignore the common glob-style patterns 'CVS', '.*', '*~' and '*.pyc'."),
        make_option('--no-wrap', action='store_true', dest='no_wrap',
            default=False, help="Don't break long message lines into several lines."),
        make_option('--no-location', action='store_true', dest='no_location',
            default=False, help="Don't write '#: filename:line' lines."),
        make_option('--no-obsolete', action='store_true', dest='no_obsolete',
            default=False, help="Remove obsolete message strings."),
        make_option('--keep-pot', action='store_true', dest='keep_pot',
            default=False, help="Keep .pot file after making messages. Useful when debugging."),
    )

    # Django 1.4+ ****************************************************************

    def handle_noargs(self, *args, **options):
        self.manager = Manager()

        if get_settings('TRANSLATIONS_MAKE_BACKUPS'):
            self.manager.backup_po_to_db()

        for domain in options['domain']:
            kwargs = deepcopy(options)
            kwargs.update({'domain': domain})
            super(Command, self).handle_noargs(*args, **kwargs)

        try:
            from django.core.management.commands.makemessages import make_messages as old_make_messages
        except ImportError:
            self.manager.postprocess()

    def write_po_file(self, potfile, locale):
        super(Command, self).write_po_file(potfile, locale)

        basedir = os.path.join(os.path.dirname(potfile), locale, 'LC_MESSAGES')
        if not os.path.isdir(basedir):
            os.makedirs(basedir)
        pofile = os.path.join(basedir, '%s.po' % str(self.domain))

        # load data from po file to db
        if os.path.dirname(potfile) in settings.LOCALE_PATHS:
            self.manager.store_to_db(pofile, locale)



# Django 1.2-1.3 *************************************************************

def make_messages(locale=None, domain=None, verbosity='1', all=False,
        extensions=None, symlinks=False, ignore_patterns=[], no_wrap=False,
        no_location=False, no_obsolete=False, stdout=sys.stdout):
    """
    Uses the locale directory from the Django SVN tree or an application/
    project to process all
    """
    import fnmatch
    import glob
    import os
    import re
    import sys
    from itertools import dropwhile
    from subprocess import PIPE, Popen

    from django.conf import settings
    from django.core.management.base import CommandError
    from django.utils.translation import templatize

    pythonize_re = re.compile(r'(?:^|\n)\s*//')
    plural_forms_re = re.compile(r'^(?P<value>"Plural-Forms.+?\\n")\s*$', re.MULTILINE | re.DOTALL)

    if domain is None:
        domain = get_settings('TRANSLATIONS_DOMAINS') or ['django', 'djangojs']

    if not isinstance(domain, list):
        domain = [domain]

    def handle_extensions(extensions=('html',)):
        """
        organizes multiple extensions that are separated with commas or passed by
        using --extension/-e multiple times.
        for example: running 'django-admin makemessages -e js,txt -e xhtml -a'
        would result in a extension list: ['.js', '.txt', '.xhtml']
        >>> handle_extensions(['.html', 'html,js,py,py,py,.py', 'py,.py'])
        ['.html', '.js']
        >>> handle_extensions(['.html, txt,.tpl'])
        ['.html', '.tpl', '.txt']
        """
        ext_list = []
        for ext in extensions:
            ext_list.extend(ext.replace(' ','').split(','))
        for i, ext in enumerate(ext_list):
            if not ext.startswith('.'):
                ext_list[i] = '.%s' % ext_list[i]

        # we don't want *.py files here because of the way non-*.py files
        # are handled in make_messages() (they are copied to file.ext.py files to
        # trick xgettext to parse them as Python files)
        return set([x for x in ext_list if x != '.py'])

    def _popen(cmd):
        """
        Friendly wrapper around Popen for Windows
        """
        p = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE, close_fds=os.name != 'nt', universal_newlines=True)
        return p.communicate()

    def walk(root, topdown=True, onerror=None, followlinks=False):
        """
        A version of os.walk that can follow symlinks for Python < 2.6
        """
        for dirpath, dirnames, filenames in os.walk(root, topdown, onerror):
            yield (dirpath, dirnames, filenames)
            if followlinks:
                for d in dirnames:
                    p = os.path.join(dirpath, d)
                    if os.path.islink(p):
                        for link_dirpath, link_dirnames, link_filenames in walk(p):
                            yield (link_dirpath, link_dirnames, link_filenames)

    def is_ignored(path, ignore_patterns):
        """
        Helper function to check if the given path should be ignored or not.
        """
        for pattern in ignore_patterns:
            if fnmatch.fnmatchcase(path, pattern):
                return True
        return False

    def find_files(root, ignore_patterns, verbosity, symlinks=False):
        """
        Helper function to get all files in the given root.
        """
        all_files = []
        for (dirpath, dirnames, filenames) in walk(".", followlinks=symlinks):
            for f in filenames:
                norm_filepath = os.path.normpath(os.path.join(dirpath, f))
                if is_ignored(norm_filepath, ignore_patterns):
                    if verbosity > 1:
                        sys.stdout.write('ignoring file %s in %s\n' % (f, dirpath))
                else:
                    all_files.extend([(dirpath, f)])
        all_files.sort()
        return all_files

    def copy_plural_forms(msgs, locale, domain, verbosity):
        """
        Copies plural forms header contents from a Django catalog of locale to
        the msgs string, inserting it at the right place. msgs should be the
        contents of a newly created .po file.
        """
        import django
        django_dir = os.path.normpath(os.path.join(os.path.dirname(django.__file__)))
        if domain == 'djangojs':
            domains = ('djangojs', 'django')
        else:
            domains = ('django',)
        for domain in domains:
            django_po = os.path.join(django_dir, 'conf', 'locale', locale, 'LC_MESSAGES', '%s.po' % domain)
            if os.path.exists(django_po):
                m = plural_forms_re.search(open(django_po, 'rU').read())
                if m:
                    if verbosity > 1:
                        sys.stderr.write("copying plural forms: %s\n" % m.group('value'))
                    lines = []
                    seen = False
                    for line in msgs.split('\n'):
                        if not line and not seen:
                            line = '%s\n' % m.group('value')
                            seen = True
                        lines.append(line)
                    msgs = '\n'.join(lines)
                    break
        return msgs

    # Need to ensure that the i18n framework is enabled
    if settings.configured:
        settings.USE_I18N = True
    else:
        settings.configure(USE_I18N = True)


    manager = Manager()
    if settings.TRANSLATIONS_MAKE_BACKUPS:
        manager.backup_po_to_db()


    invoked_for_django = False
    if os.path.isdir(os.path.join('conf', 'locale')):
        localedir = os.path.abspath(os.path.join('conf', 'locale'))
        invoked_for_django = True
    elif os.path.isdir('locale'):
        localedir = os.path.abspath('locale')
    elif 1.6 > float(django.get_version()) >= 1.4:
        localedir = os.path.abspath(os.path.join(get_settings('TRANSLATIONS_BASE_DIR'), 'locale'))
    else:
        raise CommandError("This script should be run from the Django SVN tree or your project or app tree. If you did indeed run it from the SVN checkout or your project or application, maybe you are just missing the conf/locale (in the django tree) or locale (for project and application) directory? It is not created automatically, you have to create it by hand if you want to enable i18n for your project or application.")

    for d in domain:
        if d not in ('django', 'djangojs'):
            raise CommandError("currently makemessages only supports domains 'django' and 'djangojs'")

    if (locale is None and not all) or domain is None:
        # backwards compatible error message
        if not sys.argv[0].endswith("make-messages.py"):
            message = "Type '%s help %s' for usage.\n" % (os.path.basename(sys.argv[0]), sys.argv[1])
        else:
            message = "usage: make-messages.py -l <language>\n   or: make-messages.py -a\n"
        raise CommandError(message)

    # We require gettext version 0.15 or newer.
    output = _popen('xgettext --version')[0]
    match = re.search(r'(?P<major>\d+)\.(?P<minor>\d+)', output)
    if match:
        xversion = (int(match.group('major')), int(match.group('minor')))
        if xversion < (0, 15):
            raise CommandError("Django internationalization requires GNU gettext 0.15 or newer. You are using version %s, please upgrade your gettext toolset." % match.group())

    languages = []
    if locale is not None:
        languages.append(locale)
    elif all:
        locale_dirs = filter(os.path.isdir, glob.glob('%s/*' % localedir))
        languages = [os.path.basename(l) for l in locale_dirs]

    for d in domain:
        if d == 'djangojs':
            exts = ['js']
        else:
            exts = ['html', 'txt']
        extensions = handle_extensions(exts)

        for locale in languages:
            if verbosity > 0:
                print ("processing language", locale)
            basedir = os.path.join(localedir, locale, 'LC_MESSAGES')
            if not os.path.isdir(basedir):
                os.makedirs(basedir)

            pofile = os.path.join(basedir, '%s.po' % d)
            potfile = os.path.join(basedir, '%s.pot' % d)

            if os.path.exists(potfile):
                os.unlink(potfile)

            for dirpath, file in find_files(".", ignore_patterns, verbosity, symlinks=symlinks):
                file_base, file_ext = os.path.splitext(file)
                if d == 'djangojs' and file_ext in extensions:
                    if verbosity > 1:
                        sys.stdout.write('processing file %s in %s\n' % (file, dirpath))
                    src = open(os.path.join(dirpath, file), "rU").read()
                    src = pythonize_re.sub('\n#', src)
                    thefile = '%s.py' % file
                    f = open(os.path.join(dirpath, thefile), "w")
                    try:
                        f.write(src)
                    finally:
                        f.close()
                    cmd = 'xgettext -d %s -L Perl --keyword=gettext_noop --keyword=gettext_lazy --keyword=ngettext_lazy:1,2 --from-code UTF-8 -o - "%s"' % (d, os.path.join(dirpath, thefile))
                    msgs, errors = _popen(cmd)
                    if errors:
                        raise CommandError("errors happened while running xgettext on %s\n%s" % (file, errors))
                    old = '#: '+os.path.join(dirpath, thefile)[2:]
                    new = '#: '+os.path.join(dirpath, file)[2:]
                    msgs = msgs.replace(old, new)
                    if os.path.exists(potfile):
                        # Strip the header
                        msgs = '\n'.join(dropwhile(len, msgs.split('\n')))
                    else:
                        msgs = msgs.replace('charset=CHARSET', 'charset=UTF-8')
                    if msgs:
                        f = open(potfile, 'ab')
                        try:
                            f.write(msgs)
                        finally:
                            f.close()
                    os.unlink(os.path.join(dirpath, thefile))
                elif d == 'django' and (file_ext == '.py' or file_ext in extensions):
                    thefile = file
                    if file_ext in extensions:
                        src = open(os.path.join(dirpath, file), "rU").read()
                        thefile = '%s.py' % file
                        try:
                            f = open(os.path.join(dirpath, thefile), "w")
                            try:
                                f.write(templatize(src))
                            finally:
                                f.close()
                        except SyntaxError as msg:
                            msg = "%s (file: %s)" % (msg, os.path.join(dirpath, file))
                            raise SyntaxError(msg)
                    if verbosity > 1:
                        sys.stdout.write('processing file %s in %s\n' % (file, dirpath))
                    cmd = 'xgettext -d %s -L Python --keyword=gettext_noop --keyword=gettext_lazy --keyword=ngettext_lazy:1,2 --keyword=ugettext_noop --keyword=ugettext_lazy --keyword=ungettext_lazy:1,2 --from-code UTF-8 -o - "%s"' % (
                        d, os.path.join(dirpath, thefile))
                    msgs, errors = _popen(cmd)
                    if errors:
                        raise CommandError("errors happened while running xgettext on %s\n%s" % (file, errors))

                    if thefile != file:
                        old = '#: '+os.path.join(dirpath, thefile)[2:]
                        new = '#: '+os.path.join(dirpath, file)[2:]
                        msgs = msgs.replace(old, new)
                    if os.path.exists(potfile):
                        # Strip the header
                        msgs = '\n'.join(dropwhile(len, msgs.split('\n')))
                    else:
                        msgs = msgs.replace('charset=CHARSET', 'charset=UTF-8')
                    if msgs:
                        f = open(potfile, 'ab')
                        try:
                            f.write(msgs)
                        finally:
                            f.close()
                    if thefile != file:
                        os.unlink(os.path.join(dirpath, thefile))

            if os.path.exists(potfile):
                msgs, errors = _popen('msguniq --to-code=utf-8 "%s"' % potfile)
                if errors:
                    raise CommandError("errors happened while running msguniq\n%s" % errors)
                f = open(potfile, 'w')
                try:
                    f.write(msgs)
                finally:
                    f.close()
                if os.path.exists(pofile):
                    msgs, errors = _popen('msgmerge -q "%s" "%s"' % (pofile, potfile))
                    if errors:
                        raise CommandError("errors happened while running msgmerge\n%s" % errors)
                elif not invoked_for_django:
                    msgs = copy_plural_forms(msgs, locale, d, verbosity)
                f = open(pofile, 'wb')
                try:
                    f.write(msgs)
                finally:
                    f.close()
                os.unlink(potfile)


        for locale in languages:
            basedir = os.path.join(localedir, locale, 'LC_MESSAGES')
            pofile = os.path.join(basedir, '%s.po' % d)
            manager.store_to_db(pofile, locale)

    manager.postprocess()


from django.core.management.commands import makemessages
makemessages.make_messages = make_messages

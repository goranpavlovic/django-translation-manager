from functools import update_wrapper

from django.contrib import admin
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, JsonResponse
from django.utils.translation import ugettext_lazy as _
from django.conf import settings

from .manager import Manager
from .models import TranslationEntry, TranslationBackup
from .signals import post_save
from .widgets import add_styles
from .utils import filter_queryset
from .settings import get_settings

from translation_manager import tasks

from django.core.cache import cache

filter_excluded_fields = lambda fields: [field for field in fields if field not in get_settings('TRANSLATIONS_ADMIN_EXCLUDE_FIELDS')]


class TranslationEntryAdmin(admin.ModelAdmin):
    actions_on_bottom = True
    save_on_top = True

    default_fields = ['original', 'language', 'get_hint', 'translation', 'occurrences', 'locale_parent_dir', 'domain']

    list_fields = filter_excluded_fields(get_settings('TRANSLATIONS_ADMIN_FIELDS'))
    if not list_fields:
        list_fields = filter_excluded_fields(default_fields)

    fields = default_fields
    list_display = list_fields
    list_editable = ('translation',)
    ordering = ('original', 'language')
    readonly_fields = list(default_fields)
    readonly_fields.remove('original')
    search_fields = filter_excluded_fields(['original', 'translation', 'occurrences'])
    list_per_page = 100

    from .filters import TranslationStateFilter, CustomFilter
    list_filter = ['language', 'locale_parent_dir', 'domain', TranslationStateFilter]
    if get_settings('TRANSLATIONS_CUSTOM_FILTERS'):
        list_filter.append(CustomFilter)
    else:
        list_filter = ('language', 'locale_parent_dir', 'domain')

    change_list_template = "admin/translation_manager/change_list.html"

    list_filter = filter_excluded_fields(list_filter)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['make_translations_running'] = cache.get('make_translations_running')
        return super(TranslationEntryAdmin, self).changelist_view(request, extra_context=extra_context)

    def formfield_for_dbfield(self, db_field, **kwargs):
        formfield = super(TranslationEntryAdmin, self).formfield_for_dbfield(db_field, **kwargs)
        if db_field.name == 'translation':
            add_styles(formfield.widget, u'height: 26px;')
        return formfield

    def get_urls(self):
        from django.conf.urls import url

        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        info = self.model._meta.app_label, self.model._meta.model_name

        urls = [
            url(r'^make/$', wrap(self.make_translations_view), name='%s_%s_make' % info),
            url(r'^compile/$', wrap(self.compile_translations_view), name='%s_%s_compile' % info),
            url(r'^load_from_po/$', wrap(self.load_from_po_view), name='%s_%s_load' % info),
            url(r'^get_make_translations_status/$', wrap(self.get_make_translations_status),
                name='%s_%s_status' % info)
        ]
 
        super_urls = super(TranslationEntryAdmin, self).get_urls()
 
        return urls + super_urls

    def get_queryset(self, request):
        qs = super(TranslationEntryAdmin, self).get_queryset(request=request)
        return filter_queryset(qs)

    # older django
    def queryset(self, request):
        qs = super(TranslationEntryAdmin, self).queryset(request=request)
        return filter_queryset(qs)

    def get_make_translations_status(self, request):
        if cache.get('make_translations_running'):
            result = {"running": True}
        else:
            result = {"running": False}

        return JsonResponse(result)

    def load_from_po_view(self, request):
        if request.user.has_perm('translation_manager.load'):
            manager = Manager()
            manager.load_data_from_po()

            self.message_user(request, _("admin-translation_manager-data-loaded_from_po"))
        return HttpResponseRedirect(reverse("admin:translation_manager_translationentry_changelist"))

    def make_translations_view(self, request):
        translation_mode = str(get_settings('TRANSLATIONS_PROCESSING_METHOD'))
        if not cache.get('make_translations_running'):
            cache.set('make_translations_running', True)
            if translation_mode == 'sync':
                tasks.makemessages_task()
            elif translation_mode == 'async_django_rq':
                tasks.makemessages_task.delay()
        self.message_user(request, _("admin-translation_manager-translations_made"))
        return HttpResponseRedirect(reverse("admin:translation_manager_translationentry_changelist"))

    def compile_translations_view(self, request):
        manager = Manager()
        for language, language_name in settings.LANGUAGES:
            manager.update_po_from_db(lang=language)
        post_save.send(sender=None, request=request)

        self.message_user(request, _("admin-translation_manager-translations_compiled"))
        return HttpResponseRedirect(reverse("admin:translation_manager_translationentry_changelist"))

    def get_changelist(self, request, **kwargs):

        from .views import TranslationChangeList
        return TranslationChangeList


def restore(modeladmin, request, queryset):
    for backup in queryset:
        backup.restore()
restore.short_description = _("admin-translation_manager-backups_restore_option")


class TranslationBackupAdmin(admin.ModelAdmin):
    actions_on_bottom = True
    actions = [restore]
    save_on_top = True
    fields = ('created', 'language', 'locale_parent_dir', 'domain', 'content')
    list_display = ('created', 'language', 'locale_parent_dir', 'domain')
    list_filter = ('created', 'language', 'locale_parent_dir', 'domain')
    readonly_fields = ('created', 'language', 'locale_parent_dir', 'domain')


admin.site.register(TranslationEntry, TranslationEntryAdmin)
admin.site.register(TranslationBackup, TranslationBackupAdmin)

from collections import defaultdict
import os.path

from django.conf import settings
from django.template.base import TemplateSyntaxError, Library, Node, TextNode,\
    token_kwargs, Variable
from django.template.loader import find_template, get_template, get_template_from_string, LoaderOrigin
from django.utils.safestring import mark_safe
from django.utils import six

register = Library()

BLOCK_CONTEXT_KEY = 'block_context'


class ExtendsError(Exception):
    pass


class BlockContext(object):
    def __init__(self):
        # Dictionary of FIFO queues.
        self.blocks = defaultdict(list)

    def add_blocks(self, blocks):
        for name, block in six.iteritems(blocks):
            self.blocks[name].insert(0, block)

    def pop(self, name):
        try:
            return self.blocks[name].pop()
        except IndexError:
            return None

    def push(self, name, block):
        self.blocks[name].append(block)

    def get_block(self, name):
        try:
            return self.blocks[name][-1]
        except IndexError:
            return None


class BlockNode(Node):
    def __init__(self, name, nodelist, parent=None):
        self.name, self.nodelist, self.parent = name, nodelist, parent

    def __repr__(self):
        return "<Block Node: %s. Contents: %r>" % (self.name, self.nodelist)

    def render(self, context):
        block_context = context.render_context.get(BLOCK_CONTEXT_KEY)
        with context.push():
            if block_context is None:
                context['block'] = self
                result = self.nodelist.render(context)
            else:
                push = block = block_context.pop(self.name)
                if block is None:
                    block = self
                # Create new block so we can store context without thread-safety issues.
                block = type(self)(block.name, block.nodelist)
                block.context = context
                context['block'] = block
                result = block.nodelist.render(context)
                if push is not None:
                    block_context.push(self.name, push)
        return result

    def super(self):
        if not hasattr(self, 'context'):
            raise TemplateSyntaxError(
                "'%s' object has no attribute 'context'. Did you use "
                "{{ block.super }} in a base template?" % self.__class__.__name__
            )
        render_context = self.context.render_context
        if (BLOCK_CONTEXT_KEY in render_context and
                render_context[BLOCK_CONTEXT_KEY].get_block(self.name) is not None):
            return mark_safe(self.render(self.context))
        return ''


class ExtendsNode(Node):
    """
    Allows the template ``foo/bar.html`` to extend ``foo/bar.html``,
    given that there is another version of it that can be loaded. This
    allows templates to be created in a project that extend their app
    template counterparts, or even app templates that extend other app
    templates with the same relative name/path.

    We use our own version of ``find_template``, that uses an explict
    list of template directories to search for the template, based on
    the directories that the known template loaders
    (``app_directories`` and ``filesystem``) use. This list gets stored
    in the template context, and each time a template is extended, its
    absolute path gets removed from the list, so that subsequent
    searches for the same relative name/path can find parent templates
    in other directories, which allows circular inheritance to occur.

    Django's ``app_directories``, ``filesystem``, and ``cached``
    loaders are supported. The ``eggs`` loader, and any other loaders
    which use and set ``LoaderOrigin`` should also theoretically work.
    """
    must_be_first = True
    context_name = "_EXTENDS_DIRS"

    def __init__(self, nodelist, parent_name, template_name, template_path, template_dirs=None):
        self.nodelist = nodelist
        self.parent_name = parent_name
        self.template_name = template_name
        self.template_path = template_path
        self.template_dirs = template_dirs
        self.blocks = dict((n.name, n) for n in nodelist.get_nodes_by_type(BlockNode))

    def __repr__(self):
        return '<ExtendsNode: extends %s>' % self.parent_name.token

    def populate_context(self, context):
        """
        Store a dictionary in the template context mapping template
        names to the lists of template directories available to
        search for that template. Each time a template is extended, its
        origin directory is removed from its directories list.
        """

        # These imports want settings, which aren't available when this
        # module is imported to ``add_to_builtins``, so do them here.
        from django.template.loaders.app_directories import app_template_dirs
        from django.conf import settings

        if self.context_name not in context:
            context[self.context_name] = {}
        if self.template_name not in context[self.context_name]:
            all_dirs = list(settings.TEMPLATE_DIRS) + list(app_template_dirs)
            # os.path.abspath ensures we have consistent path separators across different OSes.
            context[self.context_name][self.template_name] = list(map(os.path.abspath, all_dirs))

    def remove_template_path(self, context):
        """
        Remove template's absolute path from the context dict so
        that it won't be used again when the same relative name/path
        is requested.
        """

        remove_path = os.path.abspath(self.template_path[:-len(self.template_name) - 1])
        # The following should always succeed otherwise we have some unsupported
        # configuration - template was loaded from a source which was not added in
        # populate_context.
        try:
            context[self.context_name][self.template_name].remove(remove_path)
        except ValueError:
            # FIXME: this fails with cached loader
            if ':' not in self.template_path:
                raise

    def find_template(self, template_name, context):
        """
        Wrapper for Django's ``find_template`` that uses the current
        template context to keep track of which template directories have
        already been used when finding a template and skips them.
        This allows multiple templates with the same relative name/path to
        be discovered, so that circular template inheritance cannot occur.
        """
        dirs = context[self.context_name].get(template_name, None)
        return find_template(template_name, dirs)

    def get_template(self, template_name, context):
        """
        Similar to Django's ``get_template``, but tracking used template
        directories.
        """

        self.populate_context(context)
        self.remove_template_path(context)
        template, origin = self.find_template(template_name, context)
        if not hasattr(template, 'render'):
            # template needs to be compiled
            template = get_template_from_string(template, origin, template_name)
        return template

    def get_parent(self, context):
        parent = self.parent_name.resolve(context)
        if not parent:
            error_msg = "Invalid template name in 'extends' tag: %r." % parent
            if self.parent_name.filters or\
                    isinstance(self.parent_name.var, Variable):
                error_msg += " Got this from the '%s' variable." %\
                    self.parent_name.token
            raise TemplateSyntaxError(error_msg)
        if hasattr(parent, 'render'):
            return parent  # parent is a Template object
        return self.get_template(parent, context)

    def render(self, context):
        compiled_parent = self.get_parent(context)

        if BLOCK_CONTEXT_KEY not in context.render_context:
            context.render_context[BLOCK_CONTEXT_KEY] = BlockContext()
        block_context = context.render_context[BLOCK_CONTEXT_KEY]

        # Add the block nodes from this node to the block context
        block_context.add_blocks(self.blocks)

        # If this block's parent doesn't have an extends node it is the root,
        # and its block nodes also need to be added to the block context.
        for node in compiled_parent.nodelist:
            # The ExtendsNode has to be the first non-text node.
            if not isinstance(node, TextNode):
                if not isinstance(node, ExtendsNode):
                    blocks = dict((n.name, n) for n in
                                  compiled_parent.nodelist.get_nodes_by_type(BlockNode))
                    block_context.add_blocks(blocks)
                break

        # Call Template._render explicitly so the parser context stays
        # the same.
        return compiled_parent._render(context)


class IncludeNode(Node):
    def __init__(self, template, *args, **kwargs):
        self.template = template
        self.extra_context = kwargs.pop('extra_context', {})
        self.isolated_context = kwargs.pop('isolated_context', False)
        super(IncludeNode, self).__init__(*args, **kwargs)

    def render(self, context):
        try:
            template = self.template.resolve(context)
            # Does this quack like a Template?
            if not callable(getattr(template, 'render', None)):
                # If not, we'll try get_template
                template = get_template(template)
            values = {
                name: var.resolve(context)
                for name, var in six.iteritems(self.extra_context)
            }
            if self.isolated_context:
                return template.render(context.new(values))
            with context.push(**values):
                return template.render(context)
        except Exception:
            if settings.TEMPLATE_DEBUG:
                raise
            return ''


@register.tag('block')
def do_block(parser, token):
    """
    Define a block that can be overridden by child templates.
    """
    # token.split_contents() isn't useful here because this tag doesn't accept variable as arguments
    bits = token.contents.split()
    if len(bits) != 2:
        raise TemplateSyntaxError("'%s' tag takes only one argument" % bits[0])
    block_name = bits[1]
    # Keep track of the names of BlockNodes found in this template, so we can
    # check for duplication.
    try:
        if block_name in parser.__loaded_blocks:
            raise TemplateSyntaxError("'%s' tag with name '%s' appears more than once" % (bits[0], block_name))
        parser.__loaded_blocks.append(block_name)
    except AttributeError:  # parser.__loaded_blocks isn't a list yet
        parser.__loaded_blocks = [block_name]
    nodelist = parser.parse(('endblock',))

    # This check is kept for backwards-compatibility. See #3100.
    endblock = parser.next_token()
    acceptable_endblocks = ('endblock', 'endblock %s' % block_name)
    if endblock.contents not in acceptable_endblocks:
        parser.invalid_block_tag(endblock, 'endblock', acceptable_endblocks)

    return BlockNode(block_name, nodelist)


@register.tag('extends')
def do_extends(parser, token):
    """
    Signal that this template extends a parent template.

    This tag may be used in two ways: ``{% extends "base" %}`` (with quotes)
    uses the literal value "base" as the name of the parent template to extend,
    or ``{% extends variable %}`` uses the value of ``variable`` as either the
    name of the parent template to extend (if it evaluates to a string) or as
    the parent template itself (if it evaluates to a Template object).

    This tag that allows circular inheritance to occur, e.g. a template can
    both be overridden and extended at once.
    """
    bits = token.split_contents()
    if len(bits) != 2:
        raise TemplateSyntaxError("'%s' takes one argument" % bits[0])

    template_name = None
    template_path = None
    if hasattr(token, 'source'):
        origin, source = token.source
        if isinstance(origin, LoaderOrigin):
            template_name = origin.loadname
            template_path = origin.name

    parent_name = parser.compile_filter(bits[1])
    nodelist = parser.parse()
    if nodelist.get_nodes_by_type(ExtendsNode):
        raise TemplateSyntaxError("'%s' cannot appear more than once in the same template" % bits[0])
    return ExtendsNode(nodelist, parent_name, template_name, template_path)


@register.tag('include')
def do_include(parser, token):
    """
    Loads a template and renders it with the current context. You can pass
    additional context using keyword arguments.

    Example::

        {% include "foo/some_include" %}
        {% include "foo/some_include" with bar="BAZZ!" baz="BING!" %}

    Use the ``only`` argument to exclude the current context when rendering
    the included template::

        {% include "foo/some_include" only %}
        {% include "foo/some_include" with bar="1" only %}
    """
    bits = token.split_contents()
    if len(bits) < 2:
        raise TemplateSyntaxError("%r tag takes at least one argument: the name of the template to be included." % bits[0])
    options = {}
    remaining_bits = bits[2:]
    while remaining_bits:
        option = remaining_bits.pop(0)
        if option in options:
            raise TemplateSyntaxError('The %r option was specified more '
                                      'than once.' % option)
        if option == 'with':
            value = token_kwargs(remaining_bits, parser, support_legacy=False)
            if not value:
                raise TemplateSyntaxError('"with" in %r tag needs at least '
                                          'one keyword argument.' % bits[0])
        elif option == 'only':
            value = True
        else:
            raise TemplateSyntaxError('Unknown argument for %r tag: %r.' %
                                      (bits[0], option))
        options[option] = value
    isolated_context = options.get('only', False)
    namemap = options.get('with', {})
    return IncludeNode(parser.compile_filter(bits[1]), extra_context=namemap,
                       isolated_context=isolated_context)

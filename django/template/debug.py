from django.template.base import Lexer, Parser, tag_re, NodeList, VariableNode, TemplateSyntaxError
from django.utils.encoding import force_text
from django.utils.html import escape
from django.utils.safestring import SafeData, EscapeData
from django.utils.formats import localize
from django.utils.timezone import template_localtime


class DebugLexer(Lexer):
    pass


class DebugParser(Parser):
    def __init__(self, lexer):
        super(DebugParser, self).__init__(lexer)
        self.command_stack = []

    def enter_command(self, command, token):
        self.command_stack.append((command, token.source))

    def exit_command(self):
        self.command_stack.pop()

    def error(self, token, msg):
        return self.source_error(token.source, msg)

    def source_error(self, source, msg):
        e = TemplateSyntaxError(msg)
        e.django_template_source = source
        return e

    def create_nodelist(self):
        return DebugNodeList()

    def create_variable_node(self, contents):
        return DebugVariableNode(contents)

    def extend_nodelist(self, nodelist, node, token):
        node.source = token.source
        super(DebugParser, self).extend_nodelist(nodelist, node, token)

    def unclosed_block_tag(self, parse_until):
        command, source = self.command_stack.pop()
        msg = "Unclosed tag '%s'. Looking for one of: %s " % (command, ', '.join(parse_until))
        raise self.source_error(source, msg)

    def compile_filter_error(self, token, e):
        if not hasattr(e, 'django_template_source'):
            e.django_template_source = token.source

    def compile_function_error(self, token, e):
        if not hasattr(e, 'django_template_source'):
            e.django_template_source = token.source


class DebugNodeList(NodeList):
    def render_node(self, node, context):
        try:
            return node.render(context)
        except Exception as e:
            if not hasattr(e, 'django_template_source'):
                e.django_template_source = node.source
            raise


class DebugVariableNode(VariableNode):
    def render(self, context):
        try:
            output = self.filter_expression.resolve(context)
            output = template_localtime(output, use_tz=context.use_tz)
            output = localize(output, use_l10n=context.use_l10n)
            output = force_text(output)
        except UnicodeDecodeError:
            return ''
        except Exception as e:
            if not hasattr(e, 'django_template_source'):
                e.django_template_source = self.source
            raise
        if (context.autoescape and not isinstance(output, SafeData)) or isinstance(output, EscapeData):
            return escape(output)
        else:
            return output

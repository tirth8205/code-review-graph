"""Mini HTML rendering for MRR eval fixture."""


def esc_h(s):
    """Replace dangerous characters so the string is safe inside HTML."""
    out = s.replace("&", "&amp;")
    out = out.replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace('"', "&quot;")
    out = out.replace("`", "&#96;")
    return out


def render_html(graph):
    buf = ["<html><body><ul>"]
    for name in graph.nodes:
        buf.append("<li>" + esc_h(name) + "</li>")
    buf.append("</ul></body></html>")
    return "".join(buf)

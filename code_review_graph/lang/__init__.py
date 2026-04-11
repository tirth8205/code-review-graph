"""Per-language parsing handlers."""

from ._base import BaseLanguageHandler
from ._c_cpp import CHandler, CppHandler
from ._csharp import CSharpHandler
from ._dart import DartHandler
from ._go import GoHandler
from ._java import JavaHandler
from ._javascript import JavaScriptHandler, TsxHandler, TypeScriptHandler
from ._kotlin import KotlinHandler
from ._lua import LuaHandler, LuauHandler
from ._perl import PerlHandler
from ._php import PhpHandler
from ._python import PythonHandler
from ._r import RHandler
from ._ruby import RubyHandler
from ._rust import RustHandler
from ._scala import ScalaHandler
from ._solidity import SolidityHandler
from ._swift import SwiftHandler

ALL_HANDLERS: list[BaseLanguageHandler] = [
    GoHandler(),
    PythonHandler(),
    JavaScriptHandler(),
    TypeScriptHandler(),
    TsxHandler(),
    RustHandler(),
    CHandler(),
    CppHandler(),
    JavaHandler(),
    CSharpHandler(),
    KotlinHandler(),
    ScalaHandler(),
    SolidityHandler(),
    RubyHandler(),
    DartHandler(),
    SwiftHandler(),
    PhpHandler(),
    PerlHandler(),
    RHandler(),
    LuaHandler(),
    LuauHandler(),
]

__all__ = [
    "BaseLanguageHandler", "ALL_HANDLERS",
    "GoHandler", "PythonHandler",
    "JavaScriptHandler", "TypeScriptHandler", "TsxHandler",
    "RustHandler", "CHandler", "CppHandler",
    "JavaHandler", "CSharpHandler", "KotlinHandler",
    "ScalaHandler", "SolidityHandler",
    "RubyHandler", "DartHandler",
    "SwiftHandler", "PhpHandler", "PerlHandler",
    "RHandler", "LuaHandler", "LuauHandler",
]

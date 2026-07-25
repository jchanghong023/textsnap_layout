#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <shellapi.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define MAX_LONG_PATH_CHARS 32768

typedef struct WideBuffer {
    wchar_t *data;
    size_t length;
    size_t capacity;
} WideBuffer;

static void show_error(const wchar_t *message) {
    MessageBoxW(NULL, message, L"TextSnap Layout", MB_OK | MB_ICONERROR);
}

static int reserve(WideBuffer *buffer, size_t extra) {
    size_t required;
    size_t capacity;
    wchar_t *replacement;

    if (extra > SIZE_MAX - buffer->length - 1) {
        return 0;
    }
    required = buffer->length + extra + 1;
    if (required <= buffer->capacity) {
        return 1;
    }
    capacity = buffer->capacity ? buffer->capacity : 256;
    while (capacity < required) {
        if (capacity > SIZE_MAX / 2) {
            capacity = required;
            break;
        }
        capacity *= 2;
    }
    replacement = (wchar_t *)realloc(buffer->data, capacity * sizeof(wchar_t));
    if (replacement == NULL) {
        return 0;
    }
    buffer->data = replacement;
    buffer->capacity = capacity;
    return 1;
}

static int append_char(WideBuffer *buffer, wchar_t character) {
    if (!reserve(buffer, 1)) {
        return 0;
    }
    buffer->data[buffer->length++] = character;
    buffer->data[buffer->length] = L'\0';
    return 1;
}

static int append_text(WideBuffer *buffer, const wchar_t *text) {
    size_t length = wcslen(text);
    if (!reserve(buffer, length)) {
        return 0;
    }
    memcpy(buffer->data + buffer->length, text, length * sizeof(wchar_t));
    buffer->length += length;
    buffer->data[buffer->length] = L'\0';
    return 1;
}

/* Quote one argv element according to CommandLineToArgvW parsing rules. */
static int append_quoted_argument(WideBuffer *buffer, const wchar_t *argument) {
    const wchar_t *cursor = argument;
    size_t backslashes;

    if (buffer->length && !append_char(buffer, L' ')) {
        return 0;
    }
    if (!append_char(buffer, L'"')) {
        return 0;
    }
    while (*cursor) {
        backslashes = 0;
        while (*cursor == L'\\') {
            ++backslashes;
            ++cursor;
        }
        if (*cursor == L'"') {
            while (backslashes--) {
                if (!append_text(buffer, L"\\\\")) {
                    return 0;
                }
            }
            if (!append_text(buffer, L"\\\"")) {
                return 0;
            }
            ++cursor;
        } else {
            while (backslashes--) {
                if (!append_char(buffer, L'\\')) {
                    return 0;
                }
            }
            if (*cursor && !append_char(buffer, *cursor++)) {
                return 0;
            }
        }
    }
    cursor = argument + wcslen(argument);
    backslashes = 0;
    while (cursor > argument && cursor[-1] == L'\\') {
        ++backslashes;
        --cursor;
    }
    while (backslashes--) {
        if (!append_char(buffer, L'\\')) {
            return 0;
        }
    }
    return append_char(buffer, L'"');
}

static int join_root_path(
    const wchar_t *root,
    const wchar_t *relative,
    wchar_t *destination,
    size_t capacity
) {
    size_t root_length = wcslen(root);
    size_t relative_length = wcslen(relative);
    if (root_length + 1 + relative_length + 1 > capacity) {
        return 0;
    }
    memcpy(destination, root, root_length * sizeof(wchar_t));
    destination[root_length] = L'\\';
    memcpy(
        destination + root_length + 1,
        relative,
        (relative_length + 1) * sizeof(wchar_t)
    );
    return 1;
}

int WINAPI wWinMain(
    HINSTANCE instance,
    HINSTANCE previous_instance,
    PWSTR command_line,
    int show_command
) {
    wchar_t module_path[MAX_LONG_PATH_CHARS];
    wchar_t python_path[MAX_LONG_PATH_CHARS];
    wchar_t script_path[MAX_LONG_PATH_CHARS];
    wchar_t *separator;
    DWORD module_length;
    int argument_count = 0;
    wchar_t **arguments = NULL;
    WideBuffer child_command = {0};
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    BOOL created;
    int index;

    (void)instance;
    (void)previous_instance;
    (void)command_line;
    (void)show_command;

    module_length = GetModuleFileNameW(
        NULL, module_path, (DWORD)(sizeof(module_path) / sizeof(module_path[0]))
    );
    if (module_length == 0 || module_length >= MAX_LONG_PATH_CHARS - 1) {
        show_error(L"无法定位 TextSnap Layout 程序目录。");
        return 2;
    }
    separator = wcsrchr(module_path, L'\\');
    if (separator == NULL) {
        show_error(L"TextSnap Layout 启动路径无效。");
        return 2;
    }
    *separator = L'\0';

    if (!join_root_path(
            module_path,
            L"runtime\\pythonw.exe",
            python_path,
            MAX_LONG_PATH_CHARS
        ) ||
        !join_root_path(
            module_path,
            L"app\\main.py",
            script_path,
            MAX_LONG_PATH_CHARS
        )) {
        show_error(L"TextSnap Layout 启动路径过长。");
        return 2;
    }

    arguments = CommandLineToArgvW(GetCommandLineW(), &argument_count);
    if (arguments == NULL || argument_count < 1) {
        show_error(L"无法解析 TextSnap Layout 启动参数。");
        return 2;
    }
    if (!append_quoted_argument(&child_command, python_path) ||
        !append_quoted_argument(&child_command, L"-I") ||
        !append_quoted_argument(&child_command, L"-B") ||
        !append_quoted_argument(&child_command, script_path)) {
        show_error(L"内存不足，无法启动 TextSnap Layout。");
        LocalFree(arguments);
        free(child_command.data);
        return 2;
    }
    for (index = 1; index < argument_count; ++index) {
        if (!append_quoted_argument(&child_command, arguments[index])) {
            show_error(L"内存不足，无法转发 TextSnap Layout 参数。");
            LocalFree(arguments);
            free(child_command.data);
            return 2;
        }
    }

    startup.cb = sizeof(startup);
    created = CreateProcessW(
        python_path,              /* absolute lpApplicationName */
        child_command.data,
        NULL,
        NULL,
        FALSE,
        CREATE_UNICODE_ENVIRONMENT,
        NULL,
        module_path,
        &startup,
        &process
    );
    LocalFree(arguments);
    free(child_command.data);
    if (!created) {
        show_error(
            L"无法启动内置 Python 运行时；请确认压缩包已完整解压。"
        );
        return 3;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}

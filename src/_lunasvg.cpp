#include <cctype>
#include <climits>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>

#include <pybind11/pybind11.h>

#include <lunasvg.h>
#include <plutovg.h>

namespace py = pybind11;

namespace {

// ponytail: one process-wide lock serializes every LunaSVG call (parse, render,
// add font), so only one render runs at a time per process. LunaSVG keeps a
// global font cache and plutovg's own mutex is a no-op without <threads.h>
// (e.g. macOS). Drop the lock on platforms where plutovg's mutex is known to be real.
std::mutex g_lunasvg_mutex;

// LunaSVG builds, lays out and renders the tree recursively, so a deeply
// nested document overflows the native stack. Same default as libxml2.
constexpr int kMaxDepth = 256;

// Mirrors LunaSVG's tokenizer closely enough that comments, CDATA, DOCTYPE
// and quoted attribute values cannot hide or fake tags.
bool NestingWithinLimit(std::string_view s) {
  int depth = 0;
  auto skip_past = [&](size_t from, std::string_view end) {
    size_t n = s.find(end, from);
    return n == std::string_view::npos ? s.size() : n + end.size();
  };
  size_t i = 0;
  while ((i = s.find('<', i)) != std::string_view::npos) {
    std::string_view rest = s.substr(i);
    if (rest.substr(0, 4) == "<!--") {
      i = skip_past(i + 4, "-->");
    } else if (rest.substr(0, 9) == "<![CDATA[") {
      i = skip_past(i + 9, "]]>");
    } else if (rest.substr(0, 2) == "<?") {
      i = skip_past(i + 2, "?>");
    } else if (rest.substr(0, 2) == "<!") {
      int brackets = 0;
      for (++i; i < s.size() && (s[i] != '>' || brackets > 0); ++i) {
        if (s[i] == '[') ++brackets;
        if (s[i] == ']') --brackets;
      }
    } else if (rest.substr(0, 2) == "</") {
      depth = depth > 0 ? depth - 1 : 0;
      i = skip_past(i + 2, ">");
    } else {
      bool self_closing = false;
      for (++i; i < s.size() && s[i] != '>'; ++i) {
        if (s[i] == '"' || s[i] == '\'') {
          i = s.find(s[i], i + 1);
          if (i == std::string_view::npos) return true;  // unterminated: parser rejects it
        }
        self_closing = s[i] == '/';
      }
      if (!self_closing && ++depth > kMaxDepth) return false;
    }
  }
  return true;
}

// plutovg dashes a path with no upper bound and loops forever once one dash is
// below float precision of the path length. Like Skia, stop dashing past a
// budget: remaining elements are stroked solid.
constexpr double kMaxDashes = 1e6;

// Length of one dash period in user units; 0 means no dashing; -1 means it
// cannot be bounded here (em, ex and % depend on font size or viewport).
double DashPeriod(const std::string& value) {
  if (value.empty() || value == "none") return 0;
  double sum = 0;
  int count = 0;
  const char* p = value.c_str();
  while (*p) {
    if (std::isspace(static_cast<unsigned char>(*p)) || *p == ',') {
      ++p;
      continue;
    }
    char* end;
    double x = std::strtod(p, &end);
    if (end == p || !(x >= 0)) return -1;
    p = end;
    while (std::isalpha(static_cast<unsigned char>(*p)) || *p == '%') {
      if (*p == 'e' || *p == '%') return -1;  // em, ex, %
      ++p;  // px, pt, pc, mm, cm, in only make a dash longer
    }
    sum += x;
    ++count;
  }
  return count % 2 ? sum * 2 : sum;
}

// Upper bound of the stroked length of a geometry element in its user space.
double StrokeLength(const lunasvg::Element& e) {
  std::string data;
  double factor = 1;
  if (e.hasAttribute("d")) {
    data = e.getAttribute("d");
  } else if (e.hasAttribute("points")) {
    data = "M" + e.getAttribute("points");
    factor = 2;  // a polygon's closing edge is no longer than the rest
  } else {
    // rect, circle, ellipse, line are convex: perimeter <= bounding box perimeter.
    lunasvg::Box box = e.getBoundingBox();
    return 2.0 * (double(box.w) + double(box.h));
  }
  plutovg_path_t* path = plutovg_path_create();
  plutovg_path_parse(path, data.data(), int(data.size()));  // on error keeps the valid prefix
  double length = plutovg_path_length(path);
  plutovg_path_destroy(path);
  return factor * length;
}

void LimitDashes(const lunasvg::Element& e, double inherited, double& budget) {
  double period = inherited;
  if (e.hasAttribute("stroke-dasharray")) {
    const std::string& value = e.getAttribute("stroke-dasharray");
    if (value != "inherit") period = DashPeriod(value);
  }
  if (period != 0 && (e.hasAttribute("d") || e.hasAttribute("points") || e.children().empty())) {
    double dashes = period < 0 ? budget + 1 : StrokeLength(e) / period;
    if (dashes > budget) {
      lunasvg::Element(e).setAttribute("stroke-dasharray", "none");
    } else {
      budget -= dashes;
    }
  }
  for (const lunasvg::Node& child : e.children())
    if (child.isElement()) LimitDashes(child.toElement(), period, budget);
}

struct Document {
  // Freeing a document drops font-face references, so it also takes the lock.
  ~Document() {
    py::gil_scoped_release release;
    std::lock_guard<std::mutex> lock(g_lunasvg_mutex);
    doc.reset();
  }
  std::unique_ptr<lunasvg::Document> doc;
  float width = 0;
  float height = 0;
};

std::unique_ptr<Document> Load(py::bytes data) {
  std::string_view view = data;
  if (!NestingWithinLimit(view)) {
    PyErr_Format(PyExc_OSError, "SVG elements are nested deeper than %d levels", kMaxDepth);
    throw py::error_already_set();
  }
  auto result = std::make_unique<Document>();
  {
    py::gil_scoped_release release;
    std::lock_guard<std::mutex> lock(g_lunasvg_mutex);
    result->doc = lunasvg::Document::loadFromData(view.data(), view.size());
    if (!result->doc) return nullptr;
    double budget = kMaxDashes;
    LimitDashes(result->doc->documentElement(), 0, budget);
    result->width = result->doc->width();
    result->height = result->doc->height();
  }
  return result;
}

py::bytes Render(Document& self, int64_t width, int64_t height, float scale) {
  if (!self.doc) throw std::runtime_error("document already rendered");
  std::unique_ptr<lunasvg::Document> doc = std::move(self.doc);
  lunasvg::Bitmap bitmap;
  {
    py::gil_scoped_release release;
    std::lock_guard<std::mutex> lock(g_lunasvg_mutex);
    // LunaSVG returns a null bitmap for sizes it cannot allocate (32767 px max per side).
    if (width <= INT_MAX && height <= INT_MAX)
      bitmap = lunasvg::Bitmap(static_cast<int>(width), static_cast<int>(height));
    if (!bitmap.isNull()) {
      doc->render(bitmap, lunasvg::Matrix::scaled(scale, scale));
      bitmap.convertToRGBA();
    }
    doc.reset();
  }
  if (bitmap.isNull()) {
    PyErr_Format(PyExc_OSError, "cannot allocate a %lldx%lld bitmap", (long long)width,
                 (long long)height);
    throw py::error_already_set();
  }
  const size_t row = static_cast<size_t>(width) * 4;
  PyObject* out = PyBytes_FromStringAndSize(nullptr, row * height);
  if (!out) throw py::error_already_set();
  char* dst = PyBytes_AS_STRING(out);
  const uint8_t* src = bitmap.data();
  for (int64_t y = 0; y < height; ++y) {
    std::memcpy(dst + row * y, src, row);
    src += bitmap.stride();
  }
  return py::reinterpret_steal<py::bytes>(out);
}

}  // namespace

PYBIND11_MODULE(_lunasvg, m) {
  py::class_<Document>(m, "Document")
      .def_readonly("width", &Document::width)
      .def_readonly("height", &Document::height)
      .def("render", &Render, py::arg("width"), py::arg("height"), py::arg("scale"));

  m.def("load", &Load, py::arg("data"));

  m.def(
      "add_font_file",
      [](const std::string& path, const std::string& family, bool bold, bool italic) {
        py::gil_scoped_release release;
        std::lock_guard<std::mutex> lock(g_lunasvg_mutex);
        return lunasvg_add_font_face_from_file(family.c_str(), bold, italic, path.c_str());
      },
      py::arg("path"), py::arg("family"), py::arg("bold"), py::arg("italic"));

  m.attr("lunasvg_version") = lunasvg_version_string();
}

#include <cctype>
#include <climits>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
#include <algorithm>

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

// LunaSVG expands every <use> into a deep copy of its target, and a copied
// <use> is expanded again, so a short chain of <use> grows exponentially.
constexpr int64_t kMaxElements = 200000;

// Attribute values as LunaSVG's decodeText() sees them: character and the five
// predefined entity references; on a malformed reference it keeps the prefix.
std::string DecodeValue(std::string_view in) {
  std::string out;
  while (!in.empty()) {
    char ch = in.front();
    in.remove_prefix(1);
    if (ch != '&') {
      out.push_back(ch);
      continue;
    }
    if (!in.empty() && in.front() == '#') {
      in.remove_prefix(1);
      int base = 10;
      if (!in.empty() && in.front() == 'x') {
        base = 16;
        in.remove_prefix(1);
      }
      if (!in.empty() && in.front() == '+') in.remove_prefix(1);
      uint64_t cp = 0;
      size_t digits = 0;
      for (; digits < in.size() && std::isxdigit(static_cast<unsigned char>(in[digits])); ++digits) {
        char c = in[digits];
        int d = std::isdigit(static_cast<unsigned char>(c)) ? c - '0' : std::tolower(c) - 'a' + 10;
        if (d >= base) break;
        cp = cp * base + d;
        if (cp > UINT_MAX) return out;
      }
      if (digits == 0) return out;
      in.remove_prefix(digits);
      char c[5] = {0, 0, 0, 0, 0};
      if (cp < 0x80) {
        c[0] = char(cp);
      } else if (cp < 0x800) {
        c[1] = char((cp & 0x3F) | 0x80), c[0] = char((cp >> 6) | 0xC0);
      } else if (cp < 0x10000) {
        c[2] = char((cp & 0x3F) | 0x80), c[1] = char(((cp >> 6) & 0x3F) | 0x80),
        c[0] = char((cp >> 12) | 0xE0);
      } else if (cp < 0x200000) {
        c[3] = char((cp & 0x3F) | 0x80), c[2] = char(((cp >> 6) & 0x3F) | 0x80),
        c[1] = char(((cp >> 12) & 0x3F) | 0x80), c[0] = char((cp >> 18) | 0xF0);
      }
      out.append(c);
    } else {
      static const std::pair<std::string_view, char> names[] = {
          {"amp", '&'}, {"lt", '<'}, {"gt", '>'}, {"quot", '"'}, {"apos", '\''}};
      bool found = false;
      for (const auto& [name, value] : names) {
        if (in.substr(0, name.size()) == name) {
          in.remove_prefix(name.size());
          out.push_back(value);
          found = true;
          break;
        }
      }
      if (!found) return out;
    }
    if (in.empty() || in.front() != ';') return out;
    in.remove_prefix(1);
  }
  return out;
}

struct ScanNode {
  std::vector<int> children;
  std::vector<std::string> use_targets;  // ids referenced by href/xlink:href on <use>
};

struct Expanded {
  int64_t count;
  int height;
  bool exact;  // false if a reference cycle was cut, so the result must not be memoized
};

class StructureCheck {
 public:
  explicit StructureCheck(std::string_view s) : s_(s) {}

  // Returns an error message, or nullptr if the document is within limits.
  const char* Run() {
    Tokenize();
    memo_.assign(nodes_.size(), {-1, 0, true});
    active_.assign(nodes_.size(), false);
    if (!Expand(0, 0)) return error_;
    return nullptr;
  }

 private:
  // Mirrors LunaSVG's tokenizer closely enough that comments, CDATA, DOCTYPE
  // and quoted attribute values cannot hide or fake tags or ids.
  void Tokenize() {
    nodes_.emplace_back();  // the document
    std::vector<int> open = {0};
    size_t i = 0;
    auto skip_past = [&](size_t from, std::string_view end) {
      size_t n = s_.find(end, from);
      return n == std::string_view::npos ? s_.size() : n + end.size();
    };
    auto is_space = [](char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; };
    while ((i = s_.find('<', i)) != std::string_view::npos) {
      std::string_view rest = s_.substr(i);
      if (rest.substr(0, 4) == "<!--") {
        i = skip_past(i + 4, "-->");
      } else if (rest.substr(0, 9) == "<![CDATA[") {
        i = skip_past(i + 9, "]]>");
      } else if (rest.substr(0, 2) == "<?") {
        i = skip_past(i + 2, "?>");
      } else if (rest.substr(0, 2) == "<!") {
        int brackets = 0;
        for (++i; i < s_.size() && (s_[i] != '>' || brackets > 0); ++i) {
          if (s_[i] == '[') ++brackets;
          if (s_[i] == ']') --brackets;
        }
      } else if (rest.substr(0, 2) == "</") {
        if (open.size() > 1) open.pop_back();
        i = skip_past(i + 2, ">");
      } else {
        size_t j = i + 1;
        while (j < s_.size() && !is_space(s_[j]) && s_[j] != '/' && s_[j] != '>') ++j;
        bool is_use = s_.substr(i + 1, j - i - 1) == "use";
        int node = int(nodes_.size());
        nodes_.emplace_back();
        nodes_[open.back()].children.push_back(node);
        bool self_closing = false;
        while (j < s_.size() && s_[j] != '>') {
          if (is_space(s_[j]) || s_[j] == '/') {
            self_closing = s_[j] == '/';
            ++j;
            continue;
          }
          self_closing = false;
          size_t name_start = j;
          while (j < s_.size() && !is_space(s_[j]) && s_[j] != '=' && s_[j] != '>' && s_[j] != '/') ++j;
          std::string_view name = s_.substr(name_start, j - name_start);
          while (j < s_.size() && is_space(s_[j])) ++j;
          if (j >= s_.size() || s_[j] != '=') continue;
          ++j;
          while (j < s_.size() && is_space(s_[j])) ++j;
          if (j >= s_.size() || (s_[j] != '"' && s_[j] != '\'')) continue;
          size_t close = s_.find(s_[j], j + 1);
          if (close == std::string_view::npos) return;  // unterminated: LunaSVG rejects it
          std::string value = DecodeValue(s_.substr(j + 1, close - j - 1));
          j = close + 1;
          if (name == "id") {
            ids_[value].push_back(node);
          } else if (is_use && (name == "href" || name == "xlink:href") && !value.empty() &&
                     value[0] == '#') {
            nodes_[node].use_targets.push_back(value.substr(1));
          }
        }
        i = j;
        if (!self_closing) open.push_back(node);
      }
    }
  }

  // Size and height of the tree LunaSVG builds under `node` after expanding
  // <use>. Counts every candidate target twice: a copied <use> keeps its
  // already expanded copy and gets expanded once more.
  bool Expand(int node, int depth) {
    if (depth > kMaxDepth) return Fail("SVG elements are nested deeper than 256 levels");
    Expanded& memo = memo_[node];
    if (memo.count >= 0) {
      if (depth + memo.height > kMaxDepth)
        return Fail("SVG elements are nested deeper than 256 levels");
      result_ = memo;
      return true;
    }
    active_[node] = true;
    Expanded total = {1, 0, true};
    auto add = [&](int child, int64_t weight) {
      if (active_[child]) {  // LunaSVG stops reference cycles
        total.exact = false;
        return true;
      }
      if (!Expand(child, depth + 1)) return false;
      total.count += weight * result_.count;
      total.height = std::max(total.height, result_.height + 1);
      total.exact = total.exact && result_.exact;
      return total.count <= kMaxElements || Fail("SVG expands to too many elements");
    };
    for (int child : nodes_[node].children)
      if (!add(child, 1)) return false;
    for (const std::string& id : nodes_[node].use_targets) {
      auto it = ids_.find(id);
      if (it == ids_.end()) continue;
      for (int target : it->second)
        if (!add(target, 2)) return false;
    }
    active_[node] = false;
    if (total.exact) memo = total;
    result_ = total;
    return true;
  }

  bool Fail(const char* message) {
    error_ = message;
    return false;
  }

  std::string_view s_;
  std::vector<ScanNode> nodes_;
  std::unordered_map<std::string, std::vector<int>> ids_;
  std::vector<Expanded> memo_;
  std::vector<bool> active_;
  Expanded result_ = {0, 0, true};
  const char* error_ = nullptr;
};

// plutovg dashes a path with no upper bound and loops forever once one dash is
// below float precision of the path length. Like Skia, stop dashing past a
// budget: remaining elements are stroked solid.
constexpr double kMaxDashes = 1e6;

// Parses "<number><unit>" the way LunaSVG does; false on anything else, including
// inf, nan and hex, which strtod accepts but LunaSVG rejects.
bool ParseLength(const char*& p, double& value, std::string& unit) {
  const char* start = p;
  while (std::isdigit(static_cast<unsigned char>(*p)) || std::strchr("+-.eE", *p) && *p) ++p;
  // Give back an exponent marker that starts a unit (em, ex).
  while (p > start && (p[-1] == 'e' || p[-1] == 'E')) --p;
  if (p == start) return false;
  std::string number(start, p);
  char* end;
  value = std::strtod(number.c_str(), &end);
  if (*end || !(value >= 0)) return false;
  unit.clear();
  while (std::isalpha(static_cast<unsigned char>(*p)) || *p == '%') unit += *p++;
  return true;
}

std::string Trim(const std::string& s) {
  size_t b = s.find_first_not_of(" \t\r\n"), e = s.find_last_not_of(" \t\r\n");
  return b == std::string::npos ? "" : s.substr(b, e - b + 1);
}

// font-size in user units as LunaSVG computes it (default 12); a lower bound
// for absolute units. -1 if it cannot be resolved here.
double FontSize(const std::string& attr, double parent) {
  std::string v = Trim(attr);
  const char* p = v.c_str();
  double x;
  std::string unit;
  if (!ParseLength(p, x, unit) || *p) return -1;
  if (unit == "em") return parent < 0 ? -1 : x * parent;
  if (unit == "ex") return parent < 0 ? -1 : x * parent / 2;
  if (unit == "%") return parent < 0 ? -1 : x * parent / 100;
  return x;  // px, pt, pc, mm, cm, in only make it larger
}

// Length of one dash period in user units; 0 means no dashing; -1 means it
// cannot be bounded here (% depends on the viewport, unresolved font-size).
double DashPeriod(const std::string& value, double font_size) {
  if (value.empty() || value == "none") return 0;
  double sum = 0;
  int count = 0;
  const char* p = value.c_str();
  while (*p) {
    if (std::isspace(static_cast<unsigned char>(*p)) || *p == ',') {
      ++p;
      continue;
    }
    double x;
    std::string unit;
    if (!ParseLength(p, x, unit) || unit == "%") return -1;
    if (unit == "em" || unit == "ex") {
      // Strokes are resolved before layout stores the element's font-size, so
      // LunaSVG uses 12 or the real size depending on how often it laid out.
      double em = std::min(font_size, 12.0);
      if (!(em > 0)) return -1;
      x *= unit == "em" ? em : em / 2;
    }
    sum += x;  // other units only make a dash longer
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

// stroke-dasharray is inherited as written and resolved per element.
void LimitDashes(const lunasvg::Element& e, std::string dasharray, double font_size, double& budget) {
  if (e.hasAttribute("stroke-dasharray")) {
    const std::string& value = e.getAttribute("stroke-dasharray");
    if (value != "inherit") dasharray = value;
  }
  if (e.hasAttribute("font-size")) font_size = FontSize(e.getAttribute("font-size"), font_size);
  double period = DashPeriod(dasharray, font_size);
  if (period != 0 && (e.hasAttribute("d") || e.hasAttribute("points") || e.children().empty())) {
    double dashes = period < 0 ? budget + 1 : StrokeLength(e) / period;
    if (dashes > budget) {
      lunasvg::Element(e).setAttribute("stroke-dasharray", "none");
    } else {
      budget -= dashes;
    }
  }
  for (const lunasvg::Node& child : e.children())
    if (child.isElement()) LimitDashes(child.toElement(), dasharray, font_size, budget);
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
  if (const char* error = StructureCheck(view).Run()) {
    PyErr_SetString(PyExc_OSError, error);
    throw py::error_already_set();
  }
  auto result = std::make_unique<Document>();
  {
    py::gil_scoped_release release;
    std::lock_guard<std::mutex> lock(g_lunasvg_mutex);
    result->doc = lunasvg::Document::loadFromData(view.data(), view.size());
    if (!result->doc) return nullptr;
    double budget = kMaxDashes;
    LimitDashes(result->doc->documentElement(), "", 12, budget);
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

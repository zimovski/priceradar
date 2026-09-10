import 'dart:convert';
import 'package:http/http.dart' as http;
import 'models.dart';

const apiBase = String.fromEnvironment('API_BASE_URL', defaultValue: 'http://127.0.0.1:8000');

class Api {
  Future<SearchResponse> search(String query) async {
    final uri = Uri.parse('$apiBase/api/search').replace(queryParameters: {'q': query});
    final r = await http.get(uri);
    if (r.statusCode != 200) throw Exception(_detail(r));
    return SearchResponse.fromJson(jsonDecode(r.body));
  }
  Future<Product> product(int id) async {
    final r = await http.get(Uri.parse('$apiBase/api/products/$id'));
    if (r.statusCode != 200) throw Exception(_detail(r));
    return Product.fromJson(jsonDecode(r.body));
  }
  Future<Product> trackSearch(String query) async {
    final r = await http.post(Uri.parse('$apiBase/api/search/track'), headers: {'Content-Type':'application/json'}, body: jsonEncode({'query':query}));
    if (r.statusCode != 201) throw Exception(_detail(r));
    return Product.fromJson(jsonDecode(r.body));
  }
  Future<Product> trackExternal(String provider, String externalProductId) async {
    final r = await http.post(Uri.parse('$apiBase/api/search/track-external'), headers: {'Content-Type':'application/json'},
      body: jsonEncode({'provider_slug':provider,'external_product_id':externalProductId}));
    if (r.statusCode != 201) throw Exception(_detail(r));
    return Product.fromJson(jsonDecode(r.body));
  }
  Future<void> refreshProduct(int productId) async {
    final r = await http.post(Uri.parse('$apiBase/api/products/$productId/refresh'));
    if (r.statusCode != 200) throw Exception(_detail(r));
  }
  Future<List<HistoryPoint>> history(int productId, {int days=730}) async {
    final r = await http.get(Uri.parse('$apiBase/api/products/$productId/history?days=$days'));
    if (r.statusCode != 200) throw Exception(_detail(r));
    return (jsonDecode(r.body) as List).map((e)=>HistoryPoint.fromJson(e)).toList();
  }
  Future<PriceSummary> summary(int productId) async {
    final r = await http.get(Uri.parse('$apiBase/api/products/$productId/summary'));
    if (r.statusCode != 200) throw Exception(_detail(r));
    return PriceSummary.fromJson(jsonDecode(r.body));
  }
  String _detail(http.Response r) {
    try { return jsonDecode(r.body)['detail'] ?? 'HTTP ${r.statusCode}'; } catch (_) { return 'HTTP ${r.statusCode}'; }
  }
}

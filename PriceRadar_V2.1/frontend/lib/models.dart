class Product {
  final int id;
  final String name;
  final String? brand;
  final String? model;
  final String? imageUrl;
  Product({required this.id, required this.name, this.brand, this.model, this.imageUrl});
  factory Product.fromJson(Map<String, dynamic> j) => Product(
    id: j['id'], name: j['name'], brand: j['brand'], model: j['model'], imageUrl: j['image_url'],
  );
}

class SearchResult {
  final String resultKey;
  final String source;
  final String sourceName;
  final int? localProductId;
  final String? externalProductId;
  final String name;
  final String? brand;
  final String? model;
  final String? imageUrl;
  final double? price;
  final bool tracked;
  SearchResult({required this.resultKey, required this.source, required this.sourceName, this.localProductId,
    this.externalProductId, required this.name, this.brand, this.model, this.imageUrl, this.price, required this.tracked});
  factory SearchResult.fromJson(Map<String, dynamic> j) => SearchResult(
    resultKey: j['result_key'], source: j['source'], sourceName: j['source_name'],
    localProductId: j['local_product_id'], externalProductId: j['external_product_id'], name: j['name'],
    brand: j['brand'], model: j['model'], imageUrl: j['image_url'],
    price: (j['price'] as num?)?.toDouble(), tracked: j['tracked'] ?? false,
  );
}

class ProviderStatus {
  final String name;
  final bool configured;
  final bool ok;
  final String? message;
  ProviderStatus({required this.name, required this.configured, required this.ok, this.message});
  factory ProviderStatus.fromJson(Map<String, dynamic> j) => ProviderStatus(
    name: j['name'], configured: j['configured'], ok: j['ok'], message: j['message'],
  );
}

class SearchResponse {
  final String query;
  final List<SearchResult> results;
  final List<ProviderStatus> providers;
  SearchResponse({required this.query, required this.results, required this.providers});
  factory SearchResponse.fromJson(Map<String, dynamic> j) => SearchResponse(
    query: j['query'],
    results: (j['results'] as List).map((e)=>SearchResult.fromJson(e)).toList(),
    providers: (j['providers'] as List).map((e)=>ProviderStatus.fromJson(e)).toList(),
  );
}

class HistoryPoint {
  final DateTime date;
  final String retailer;
  final String retailerSlug;
  final double price;
  final String sourceKind;
  HistoryPoint({required this.date, required this.retailer, required this.retailerSlug, required this.price, required this.sourceKind});
  factory HistoryPoint.fromJson(Map<String, dynamic> j) => HistoryPoint(
    date: DateTime.parse(j['date']), retailer: j['retailer'], retailerSlug: j['retailer_slug'],
    price: (j['price'] as num).toDouble(), sourceKind: j['source_kind'],
  );
}

class PriceSummary {
  final double? currentMin;
  final double? historicalMin;
  final double? historicalMax;
  final double? average;
  final int observations;
  PriceSummary({this.currentMin, this.historicalMin, this.historicalMax, this.average, required this.observations});
  factory PriceSummary.fromJson(Map<String, dynamic> j) => PriceSummary(
    currentMin: (j['current_min'] as num?)?.toDouble(), historicalMin: (j['historical_min'] as num?)?.toDouble(),
    historicalMax: (j['historical_max'] as num?)?.toDouble(), average: (j['average'] as num?)?.toDouble(),
    observations: j['observations'] ?? 0,
  );
}

import 'package:flutter/material.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:intl/intl.dart';
import 'api.dart';
import 'models.dart';

void main() => runApp(const PriceRadarApp());

class PriceRadarApp extends StatelessWidget {
  const PriceRadarApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
    debugShowCheckedModeBanner: false,
    title: 'PriceRadar',
    theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.indigo),
    home: const HomePage(),
  );
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final api = Api();
  final controller = TextEditingController();
  SearchResponse? response;
  bool loading = false;
  String? error;

  Future<void> search() async {
    final q = controller.text.trim();
    if (q.length < 2) return;
    setState(() { loading = true; error = null; });
    try {
      final r = await api.search(q);
      if (!mounted) return;
      setState(() { response = r; loading = false; });
    } catch (e) {
      if (!mounted) return;
      setState(() { error = e.toString(); loading = false; });
    }
  }

  Future<void> openResult(SearchResult r) async {
    try {
      Product p;
      if (r.localProductId != null) {
        p = await api.product(r.localProductId!);
      } else {
        p = await api.trackExternal(r.source, r.externalProductId!);
      }
      if (!mounted) return;
      Navigator.push(context, MaterialPageRoute(builder: (_) => ProductPage(product: p)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.toString())));
    }
  }

  Future<void> trackText() async {
    final q = controller.text.trim();
    if (q.length < 2) return;
    try {
      final p = await api.trackSearch(q);
      if (!mounted) return;
      Navigator.push(context, MaterialPageRoute(builder: (_) => ProductPage(product: p)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.toString())));
    }
  }

  @override void dispose() { controller.dispose(); super.dispose(); }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('PriceRadar')),
    body: ListView(padding: const EdgeInsets.all(16), children: [
      Text('O que você quer comprar?', style: Theme.of(context).textTheme.headlineMedium),
      const SizedBox(height: 8),
      const Text('Pesquise por texto. Lojas conectadas entram no mesmo resultado.'),
      const SizedBox(height: 16),
      SearchBar(
        controller: controller,
        hintText: 'PlayStation 5 Slim Digital, RTX 5070...',
        leading: const Icon(Icons.search),
        trailing: [IconButton(onPressed: loading ? null : search, icon: const Icon(Icons.arrow_forward))],
        onSubmitted: (_) => search(),
      ),
      if (loading) const Padding(padding: EdgeInsets.only(top: 12), child: LinearProgressIndicator()),
      if (error != null) Padding(padding: const EdgeInsets.only(top: 12), child: Text(error!, style: TextStyle(color: Theme.of(context).colorScheme.error))),
      if (response != null) ...[
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: response!.providers.map((p) => Chip(
          avatar: Icon(p.ok ? Icons.check_circle : Icons.info_outline, size: 18),
          label: Text(p.ok ? '${p.name}: conectado' : '${p.name}: ${p.message ?? 'indisponível'}'),
        )).toList()),
        const SizedBox(height: 10),
        ...response!.results.map((r) => Card(child: Padding(
          padding: const EdgeInsets.all(10),
          child: Row(children: [
            if (r.imageUrl != null) ClipRRect(borderRadius: BorderRadius.circular(8), child: Image.network(r.imageUrl!, width: 72, height: 72, fit: BoxFit.contain))
            else const SizedBox(width: 72, height: 72, child: Icon(Icons.inventory_2_outlined)),
            const SizedBox(width: 12),
            Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(r.name, style: const TextStyle(fontWeight: FontWeight.bold)),
              const SizedBox(height: 3),
              Text([r.brand, r.model].whereType<String>().join(' • ')),
              if (r.price != null) Padding(padding: const EdgeInsets.only(top: 6), child: Text(money(r.price), style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold))),
              Text(r.sourceName, style: Theme.of(context).textTheme.bodySmall),
            ])),
            FilledButton(onPressed: () => openResult(r), child: Text(r.localProductId != null ? 'Abrir' : 'Acompanhar')),
          ]),
        ))),
        Card(child: ListTile(
          leading: const Icon(Icons.bookmark_add_outlined),
          title: Text('Acompanhar apenas a busca “${response!.query}”'),
          subtitle: const Text('Cria o item sem inventar preço; útil enquanto outras lojas ainda não estão conectadas.'),
          trailing: OutlinedButton(onPressed: trackText, child: const Text('Acompanhar')),
        )),
      ],
    ]),
  );
}

String money(double? v) => v == null ? '—' : NumberFormat.currency(locale: 'pt_BR', symbol: 'R\$').format(v);

class ProductPage extends StatefulWidget {
  final Product product;
  const ProductPage({super.key, required this.product});
  @override State<ProductPage> createState() => _ProductPageState();
}

class _ProductPageState extends State<ProductPage> {
  final api = Api();
  int days = 90;
  late Future<List<HistoryPoint>> historyFuture;
  late Future<PriceSummary> summaryFuture;
  bool refreshing = false;
  @override void initState() { super.initState(); load(); }
  void load() { historyFuture = api.history(widget.product.id, days: days); summaryFuture = api.summary(widget.product.id); }
  void setDays(int value) { setState(() { days = value; load(); }); }
  Future<void> refresh() async {
    setState(() => refreshing = true);
    try { await api.refreshProduct(widget.product.id); if (mounted) setState(load); }
    catch (e) { if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.toString()))); }
    finally { if (mounted) setState(() => refreshing = false); }
  }
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(widget.product.name), actions: [IconButton(onPressed: refreshing ? null : refresh, icon: const Icon(Icons.refresh))]),
    body: ListView(padding: const EdgeInsets.all(16), children: [
      FutureBuilder<PriceSummary>(future: summaryFuture, builder: (context, snap) {
        final s = snap.data;
        return Wrap(spacing: 12, runSpacing: 12, children: [
          Metric('Menor atual', money(s?.currentMin)), Metric('Menor histórico', money(s?.historicalMin)),
          Metric('Média', money(s?.average)), Metric('Máximo', money(s?.historicalMax)),
        ]);
      }),
      const SizedBox(height: 18),
      Wrap(spacing: 8, children: [for (final d in [7,30,90,180,365,730]) ChoiceChip(label: Text(d==730?'2A':d==365?'1A':'${d}D'), selected: days==d, onSelected: (_) => setDays(d))]),
      const SizedBox(height: 16),
      FutureBuilder<List<HistoryPoint>>(future: historyFuture, builder: (context, snap) {
        if (!snap.hasData) return const SizedBox(height: 280, child: Center(child: CircularProgressIndicator()));
        final items = snap.data!;
        if (items.isEmpty) return const Card(child: Padding(padding: EdgeInsets.all(24), child: Text('Ainda não há observações de preço para este produto.')));
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(height: 300, child: PriceChart(items: items)),
          const SizedBox(height: 18),
          Text('Tabela de preços', style: Theme.of(context).textTheme.titleLarge),
          SingleChildScrollView(scrollDirection: Axis.horizontal, child: DataTable(columns: const [
            DataColumn(label: Text('Data')), DataColumn(label: Text('Loja')), DataColumn(label: Text('Preço')), DataColumn(label: Text('Fonte')),
          ], rows: items.reversed.take(100).map((e)=>DataRow(cells:[
            DataCell(Text(DateFormat('dd/MM/yy HH:mm').format(e.date.toLocal()))), DataCell(Text(e.retailer)), DataCell(Text(money(e.price))), DataCell(Text(e.sourceKind)),
          ])).toList())),
        ]);
      }),
    ]),
  );
}

class Metric extends StatelessWidget {
  final String label, value; const Metric(this.label, this.value, {super.key});
  @override Widget build(BuildContext context) => SizedBox(width: 180, child: Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children:[Text(label), const SizedBox(height:4), Text(value, style: const TextStyle(fontSize:22,fontWeight:FontWeight.bold))]))));
}

class PriceChart extends StatelessWidget {
  final List<HistoryPoint> items; const PriceChart({super.key, required this.items});
  @override Widget build(BuildContext context) {
    final grouped=<String,List<HistoryPoint>>{}; for(final p in items){grouped.putIfAbsent(p.retailerSlug,()=>[]).add(p);} final minDate=items.map((e)=>e.date.millisecondsSinceEpoch.toDouble()).reduce((a,b)=>a<b?a:b);
    final colors=[Colors.blue,Colors.green,Colors.orange,Colors.purple,Colors.red]; int i=0;
    final bars=grouped.entries.map((entry){final color=colors[(i++)%colors.length];final sorted=[...entry.value]..sort((a,b)=>a.date.compareTo(b.date));return LineChartBarData(spots:sorted.map((p)=>FlSpot((p.date.millisecondsSinceEpoch.toDouble()-minDate)/86400000.0,p.price)).toList(),isCurved:false,dotData:const FlDotData(show:true),barWidth:2,color:color);}).toList();
    return Card(child: Padding(padding: const EdgeInsets.fromLTRB(12,20,20,12), child: LineChart(LineChartData(lineBarsData:bars,titlesData:const FlTitlesData(topTitles:AxisTitles(sideTitles:SideTitles(showTitles:false)),rightTitles:AxisTitles(sideTitles:SideTitles(showTitles:false))),gridData:const FlGridData(show:true),borderData:FlBorderData(show:false)))));
  }
}

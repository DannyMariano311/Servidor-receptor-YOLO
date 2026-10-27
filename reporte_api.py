import logging
from aiohttp import web
from datetime import datetime
from collections import defaultdict

import webrtc_session

logger = logging.getLogger(__name__)

async def get_reporte(request: web.Request) -> web.Response:
    fecha_inicio = request.query.get("fecha_inicio")
    fecha_fin = request.query.get("fecha_fin")
    
    if not fecha_inicio or not fecha_fin:
        return web.json_response({"error": "fecha_inicio and fecha_fin are required. Format YYYY-MM-DD"}, status=400)
    
    try:
        start_date = datetime.strptime(fecha_inicio, "%Y-%m-%d")
        end_date = datetime.strptime(fecha_fin, "%Y-%m-%d")
    except ValueError:
         return web.json_response({"error": "Invalid date format. Use YYYY-MM-DD"}, status=400)

    pool = webrtc_session.db_pool
    if not pool:
        return web.json_response({"error": "DB Pool not initialized"}, status=500)

    # 1. Obtener eventos y su lugar usando PostGIS
    query_eventos = """
    SELECT 
        e.id as evento_id,
        e.tipo_evento, 
        e.timestamp_evento, 
        l.id as lugar_id,
        l.nombre as lugar_nombre,
        l.es_zona_especifica
    FROM eventos_pasajeros e
    LEFT JOIN LATERAL (
        SELECT id, nombre, es_zona_especifica
        FROM lugares
        WHERE ST_DWithin(e.coordenada::geography, lugares.coordenada::geography, lugares.radio_metros)
        ORDER BY ST_Distance(e.coordenada, lugares.coordenada) ASC
        LIMIT 1
    ) l ON true
    WHERE e.timestamp_evento >= $1 AND e.timestamp_evento < $2::date + interval '1 day'
    ORDER BY e.timestamp_evento ASC
    """
    
    query_tarifas = """
        SELECT 
            t.origen_id, 
            lo.nombre as origen_nombre,
            t.destino_id, 
            ld.nombre as destino_nombre,
            t.precio 
        FROM tarifas t
        JOIN lugares lo ON t.origen_id = lo.id
        JOIN lugares ld ON t.destino_id = ld.id
    """
    
    async with pool.acquire() as conn:
        eventos = await conn.fetch(query_eventos, start_date, end_date)
        tarifas_rows = await conn.fetch(query_tarifas)
        
    # Mapear tarifas
    tarifas = {}
    nombres_lugares = {}
    for r in tarifas_rows:
        tarifas[(r['origen_id'], r['destino_id'])] = r['precio']
        nombres_lugares[r['origen_id']] = r['origen_nombre']
        nombres_lugares[r['destino_id']] = r['destino_nombre']
        
    for ev in eventos:
        if ev['lugar_id'] is not None:
            nombres_lugares[ev['lugar_id']] = ev['lugar_nombre']
        
    anulaciones = set()
    
    # Agrupar eventos por "paradas" (eventos en el mismo lugar con poca diferencia de tiempo)
    paradas = []
    
    for ev in eventos:
        if not paradas:
            paradas.append({'lugar_id': ev['lugar_id'], 'eventos': [ev]})
        else:
            last_parada = paradas[-1]
            last_ev = last_parada['eventos'][-1]
            
            # Misma parada si el lugar es igual y paso menos de 5 min (300 s)
            time_diff = (ev['timestamp_evento'] - last_ev['timestamp_evento']).total_seconds()
            
            if ev['lugar_id'] == last_parada['lugar_id'] and time_diff < 300:
                last_parada['eventos'].append(ev)
            else:
                paradas.append({'lugar_id': ev['lugar_id'], 'eventos': [ev]})
                
    # Procesar anulaciones (máximo 1) en Zonas Específicas
    for parada in paradas:
        lugar_id = parada['lugar_id']
        if not lugar_id:
            continue
            
        es_especifica = parada['eventos'][0]['es_zona_especifica']
        
        if es_especifica:
            entradas = [e for e in parada['eventos'] if e['tipo_evento'] == 'ENTRADA']
            salidas = [e for e in parada['eventos'] if e['tipo_evento'] == 'SALIDA']
            
            if entradas and salidas:
                # Anular max 1 par
                anulaciones.add(entradas[0]['evento_id'])
                anulaciones.add(salidas[0]['evento_id'])
                
    # Simulacion FIFO
    pasajeros_a_bordo = [] # Lista de lugar_id de origen
    
    total_recaudo = 0
    desglose_tramos = defaultdict(lambda: {'cantidad': 0, 'subtotal': 0})
    
    for ev in eventos:
        if ev['evento_id'] in anulaciones:
            continue
            
        lugar_id = ev['lugar_id']
        if ev['tipo_evento'] == 'ENTRADA':
            pasajeros_a_bordo.append(lugar_id)
        elif ev['tipo_evento'] == 'SALIDA':
            if pasajeros_a_bordo:
                origen_id = pasajeros_a_bordo.pop(0)
                
                precio = tarifas.get((origen_id, lugar_id), 0)
                total_recaudo += precio
                
                # key is a tuple to keep types
                key = (origen_id, lugar_id)
                desglose_tramos[key]['cantidad'] += 1
                desglose_tramos[key]['subtotal'] += precio
                
    desglose_list = []
    for k, v in desglose_tramos.items():
        origen_id, destino_id = k
        desglose_list.append({
            "origen_nombre": nombres_lugares.get(origen_id, f"Desconocido ({origen_id})") if origen_id else "En Ruta",
            "destino_nombre": nombres_lugares.get(destino_id, f"Desconocido ({destino_id})") if destino_id else "En Ruta",
            "cantidad": v['cantidad'],
            "subtotal": v['subtotal']
        })
        
    return web.json_response({
        "total_recaudo": total_recaudo,
        "anulaciones_realizadas": len(anulaciones) // 2,
        "desglose_tramos": desglose_list
    })

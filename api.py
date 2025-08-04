from flask import jsonify, request
from sqlalchemy import text, and_, or_
from datetime import datetime, timedelta
import logging

@app.route('/api/companies/<int:company_id>/alerts/low-stock', methods=['GET'])
def get_low_stock_alerts(company_id):
    """
    Get low stock alerts for a company across all warehouses.
    
    Business Rules:
    - Low stock threshold varies by product category
    - Only alert for products with recent sales activity (last 90 days)
    - Include supplier information for reordering
    - Calculate days until stockout based on recent sales velocity
    """
    
    try:
        # Authentication check
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required"}), 401
        
        # Authorization check - ensure user belongs to the company
        if current_user.company_id != company_id:
            return jsonify({"error": "Access denied"}), 403
        
        # Get query parameters for filtering
        warehouse_ids = request.args.getlist('warehouse_ids', type=int)
        category_ids = request.args.getlist('category_ids', type=int)
        
        # Define recent sales period (configurable)
        recent_sales_days = request.args.get('recent_sales_days', 90, type=int)
        recent_sales_cutoff = datetime.utcnow() - timedelta(days=recent_sales_days)
        
        # Build the complex query using SQLAlchemy
        alerts_query = """
        WITH recent_sales_velocity AS (
            SELECT 
                si.product_id,
                si.warehouse_id,
                COALESCE(SUM(si.quantity), 0) as total_sold,
                COALESCE(SUM(si.quantity) / NULLIF(:recent_days, 0), 0) as daily_velocity,
                COUNT(DISTINCT s.sale_date::date) as sales_days
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.id
            WHERE s.company_id = :company_id 
                AND s.sale_date >= :recent_cutoff
                AND s.status = 'completed'
            GROUP BY si.product_id, si.warehouse_id
        ),
        product_thresholds AS (
            SELECT 
                p.id as product_id,
                COALESCE(pc.low_stock_threshold, 10) as threshold
            FROM products p
            LEFT JOIN product_categories pc ON p.category_id = pc.id
            WHERE p.company_id = :company_id
        )
        SELECT DISTINCT
            p.id as product_id,
            p.name as product_name,
            p.sku,
            w.id as warehouse_id,
            w.name as warehouse_name,
            i.quantity as current_stock,
            pt.threshold,
            rsv.daily_velocity,
            rsv.total_sold,
            rsv.sales_days,
            -- Calculate days until stockout (0 if no sales velocity)
            CASE 
                WHEN rsv.daily_velocity > 0 THEN 
                    ROUND(i.quantity / rsv.daily_velocity)
                ELSE 
                    999 -- High number if no recent sales
            END as days_until_stockout,
            -- Get primary supplier info
            s.id as supplier_id,
            s.name as supplier_name,
            s.contact_email as supplier_email,
            sp.supplier_sku,
            sp.supplier_price,
            sp.lead_time_days
        FROM products p
        JOIN inventory i ON p.id = i.product_id
        JOIN warehouses w ON i.warehouse_id = w.id
        JOIN product_thresholds pt ON p.id = pt.product_id
        LEFT JOIN recent_sales_velocity rsv ON p.id = rsv.product_id AND i.warehouse_id = rsv.warehouse_id
        LEFT JOIN supplier_products sp ON p.id = sp.product_id AND sp.is_primary_supplier = true
        LEFT JOIN suppliers s ON sp.supplier_id = s.id
        WHERE 
            p.company_id = :company_id
            AND w.company_id = :company_id
            AND p.is_active = true
            AND w.is_active = true
            AND i.quantity <= pt.threshold
            AND rsv.total_sold > 0  -- Only products with recent sales
            -- Optional warehouse filtering
            AND (:warehouse_filter = false OR w.id = ANY(:warehouse_ids))
            -- Optional category filtering  
            AND (:category_filter = false OR p.category_id = ANY(:category_ids))
        ORDER BY 
            (i.quantity::float / NULLIF(pt.threshold, 0)) ASC,  -- Most critical first
            rsv.daily_velocity DESC  -- Then by sales velocity
        """
        
        # Execute query with parameters
        result = db.session.execute(text(alerts_query), {
            'company_id': company_id,
            'recent_days': recent_sales_days,
            'recent_cutoff': recent_sales_cutoff,
            'warehouse_filter': len(warehouse_ids) > 0,
            'warehouse_ids': warehouse_ids or [0],
            'category_filter': len(category_ids) > 0,
            'category_ids': category_ids or [0]
        })
        
        # Process results
        alerts = []
        for row in result:
            alert = {
                "product_id": row.product_id,
                "product_name": row.product_name,
                "sku": row.sku,
                "warehouse_id": row.warehouse_id,
                "warehouse_name": row.warehouse_name,
                "current_stock": row.current_stock,
                "threshold": row.threshold,
                "days_until_stockout": min(row.days_until_stockout, 999),  # Cap at 999
                "sales_velocity": {
                    "daily_average": float(row.daily_velocity or 0),
                    "total_recent_sales": row.total_sold or 0,
                    "active_sales_days": row.sales_days or 0
                }
            }
            
            # Add supplier information if available
            if row.supplier_id:
                alert["supplier"] = {
                    "id": row.supplier_id,
                    "name": row.supplier_name,
                    "contact_email": row.supplier_email,
                    "supplier_sku": row.supplier_sku,
                    "supplier_price": float(row.supplier_price) if row.supplier_price else None,
                    "lead_time_days": row.lead_time_days
                }
            else:
                alert["supplier"] = None
                
            alerts.append(alert)
        
        # Calculate summary statistics
        total_alerts = len(alerts)
        critical_alerts = len([a for a in alerts if a["days_until_stockout"] <= 7])
        
        response = {
            "alerts": alerts,
            "total_alerts": total_alerts,
            "critical_alerts": critical_alerts,
            "summary": {
                "recent_sales_period_days": recent_sales_days,
                "timestamp": datetime.utcnow().isoformat(),
                "company_id": company_id
            }
        }
        
        # Log for monitoring
        if total_alerts > 0:
            app.logger.info(f"Low stock alerts generated: {total_alerts} total, {critical_alerts} critical for company {company_id}")
        
        return jsonify(response), 200
        
    except Exception as e:
        app.logger.error(f"Error generating low stock alerts for company {company_id}: {str(e)}")
        return jsonify({"error": "Failed to generate alerts"}), 500


# Helper endpoint to update low stock thresholds
@app.route('/api/companies/<int:company_id>/products/<int:product_id>/threshold', methods=['PUT'])
def update_product_threshold(company_id, product_id):
    """Update low stock threshold for a specific product"""
    
    try:
        if not current_user.is_authenticated or current_user.company_id != company_id:
            return jsonify({"error": "Access denied"}), 403
            
        data = request.get_json()
        new_threshold = data.get('threshold')
        
        if not isinstance(new_threshold, int) or new_threshold < 0:
            return jsonify({"error": "Threshold must be a non-negative integer"}), 400
        
        # Find product and update its category threshold
        product = Product.query.filter_by(id=product_id, company_id=company_id).first()
        if not product:
            return jsonify({"error": "Product not found"}), 404
        
        # If product has a category, update category threshold
        # Otherwise, we'd need a product-specific threshold table
        if product.category_id:
            category = ProductCategory.query.get(product.category_id)
            category.low_stock_threshold = new_threshold
            db.session.commit()
            
            return jsonify({
                "message": "Threshold updated successfully",
                "product_id": product_id,
                "new_threshold": new_threshold
            }), 200
        else:
            return jsonify({"error": "Product must have a category to set threshold"}), 400
            
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error updating threshold: {str(e)}")
        return jsonify({"error": "Failed to update threshold"}), 500


# Utility function for testing/debugging
def calculate_stock_velocity(product_id, warehouse_id, days=90):
    """Calculate sales velocity for a product in a warehouse"""
    
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    
    result = db.session.execute(text("""
        SELECT 
            COALESCE(SUM(si.quantity), 0) as total_sold,
            COUNT(DISTINCT s.sale_date::date) as sales_days
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.id
        WHERE si.product_id = :product_id 
            AND si.warehouse_id = :warehouse_id
            AND s.sale_date >= :cutoff_date
            AND s.status = 'completed'
    """), {
        'product_id': product_id,
        'warehouse_id': warehouse_id,
        'cutoff_date': cutoff_date
    }).first()
    
    total_sold = result.total_sold or 0
    sales_days = result.sales_days or 0
    
    # Calculate daily velocity
    daily_velocity = total_sold / max(days, 1)
    
    return {
        'total_sold': total_sold,
        'sales_days': sales_days,
        'daily_velocity': daily_velocity,
        'period_days': days
    }

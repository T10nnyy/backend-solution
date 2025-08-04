from flask import request, jsonify
from sqlalchemy.exc import IntegrityError
from marshmallow import Schema, fields, ValidationError
import re

# Input validation schema
class ProductCreateSchema(Schema):
    name = fields.Str(required=True, validate=fields.Length(min=1, max=255))
    sku = fields.Str(required=True, validate=fields.Length(min=1, max=50))
    price = fields.Decimal(required=True, validate=fields.Range(min=0))
    warehouse_id = fields.Int(required=True)
    initial_quantity = fields.Int(required=True, validate=fields.Range(min=0))
    company_id = fields.Int(required=True)  # Added for proper authorization

def validate_sku_format(sku):
    """Validate SKU follows expected format (alphanumeric + hyphens)"""
    if not re.match(r'^[A-Za-z0-9\-]+$', sku):
        raise ValidationError("SKU must contain only letters, numbers, and hyphens")

@app.route('/api/products', methods=['POST'])
def create_product():
    schema = ProductCreateSchema()
    
    try:
        # Validate input data
        data = schema.load(request.json)
        validate_sku_format(data['sku'])
        
        # Check authentication (simplified - should use proper auth middleware)
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required"}), 401
        
        # Verify warehouse belongs to user's company
        warehouse = Warehouse.query.filter_by(
            id=data['warehouse_id'], 
            company_id=data['company_id']
        ).first()
        
        if not warehouse:
            return jsonify({"error": "Invalid warehouse ID"}), 400
        
        # Check SKU uniqueness
        existing_product = Product.query.filter_by(sku=data['sku']).first()
        if existing_product:
            return jsonify({"error": "SKU already exists"}), 409
        
        # Begin transaction
        db.session.begin()
        
        try:
            # Create product (removed warehouse_id as products exist across warehouses)
            product = Product(
                name=data['name'],
                sku=data['sku'],
                price=data['price'],
                company_id=data['company_id']
            )
            db.session.add(product)
            db.session.flush()  # Get product.id without committing
            
            # Create initial inventory record
            inventory = Inventory(
                product_id=product.id,
                warehouse_id=data['warehouse_id'],
                quantity=data['initial_quantity']
            )
            db.session.add(inventory)
            
            # Create inventory history record
            inventory_history = InventoryHistory(
                product_id=product.id,
                warehouse_id=data['warehouse_id'],
                change_type='INITIAL_STOCK',
                quantity_change=data['initial_quantity'],
                new_quantity=data['initial_quantity'],
                created_by=current_user.id
            )
            db.session.add(inventory_history)
            
            # Commit transaction
            db.session.commit()
            
            return jsonify({
                "message": "Product created successfully",
                "product_id": product.id,
                "sku": product.sku
            }), 201
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error creating product: {str(e)}")
            return jsonify({"error": "Failed to create product"}), 500
            
    except ValidationError as e:
        return jsonify({"error": "Validation failed", "details": e.messages}), 400
    
    except IntegrityError as e:
        db.session.rollback()
        if "sku" in str(e.orig):
            return jsonify({"error": "SKU already exists"}), 409
        return jsonify({"error": "Database constraint violation"}), 400
    
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500
